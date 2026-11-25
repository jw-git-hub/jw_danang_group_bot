#!/usr/bin/env python3
"""Меню-навигатор группы → закреп в General.

Ручная команда, не cron. Публикует один rich-пост в General: анимация-шапка,
заголовок, вводные строки, таблица «Раздел | Что там» со ссылками на темы и блок
правил, — и закрепляет его без уведомления. При повторном запуске пост не
дублируется: если меню изменилось — правится тот же message_id, если нет —
ничего не отправляется.

Данные — menu/menu.json и анимация menu/radar.mp4; message_id поста — в menu_state.json
(форма как у каталога: {chat_id: {"1": запись}}, ключ 1 — General).

Запуск: python3 menu_bot.py [--dry-run] [--test] [--init]
  --dry-run — без сети и без записи state: показать план (send/edit/skip/pin) и rich HTML.
  --test    — публиковать в telegram.test_chat_id; state ведётся отдельно, под его id.
  --init    — разрешить старт без menu_state.json и для чата, которого в state ещё нет
            (первая публикация меню в группу или в тестовый чат).
Коды выхода: 0 — успех / без изменений / лок занят другим запуском;
1 — сбой, исход отправки неизвестен, меню или state не годятся, нет state без --init;
2 — ошибка аргументов, а также «опубликовано, но state не сохранён» (как у catalog_bot):
    пост в Telegram уже есть, запись о нём в state нужно внести вручную из лога (CRITICAL).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from pinned_post_sync import (
    PinnedPostSync,
    SyncOutcome,
    SyncTexts,
    format_outcome_line,
    format_plan_line,
)
from post_state import (
    ConfigError,
    StateError,
    StateStore,
    StateWording,
    acquire_lock,
    compute_content_hash,
    is_int,
    is_text,
    open_store,
    plan_steps,
    resolve_chat_key,
    resolve_inside,
)
from telegram_sender import (
    ANIMATION_TAG,
    edit_rich_message,
    escape_html,
    pin_message,
    send_rich_message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("menu_bot")

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
MENU_DIR = BASE_DIR / "menu"
MENU_PATH = MENU_DIR / "menu.json"
STATE_PATH = BASE_DIR / "menu_state.json"
LOCK_PATH = STATE_PATH.with_name(STATE_PATH.name + ".lock")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_STATE_NOT_SAVED = 2

# Меню живёт в General: в state его ключ — 1, а в запросах message_thread_id не передаётся.
GENERAL_THREAD_ID = 1
OUTCOME_LABEL = "Меню (General)"
ANIMATION_CAPTION = "анимация"

# Имя группы: @ и username Telegram (5–32 символа, латиница, цифры, «_», первая — буква).
CHAT_NAME_PATTERN = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}")
ANIMATION_SUFFIX = ".mp4"
MIN_SECTION_THREAD_ID = 1
SECTION_TEXT_FIELDS = ("title", "description")
TOPIC_URL_TEMPLATE = "https://t.me/{chat_name}/{thread_id}"
# Лимит Telegram на текст rich-сообщения.
RICH_HTML_MAX_LENGTH = 32768

# Оформление поста меняется только здесь. Видео-тег в начало ставит отправка.
TITLE_TEMPLATE = "<h3>{}</h3>"
INTRO_TEMPLATE = "<p>{}</p>"
TABLE_OPEN_TAG = "<table bordered striped>"
TABLE_HEADER_ROW = "<tr><th>Раздел</th><th>Что там</th></tr>"
TABLE_CLOSE_TAG = "</table>"
ROW_TEMPLATE = '<tr><td><a href="{url}">{title}</a></td><td>{description}</td></tr>'
RULES_TEMPLATE = "<blockquote>{}</blockquote>"
RULES_LINE_BREAK = "<br/>"

STATE_WORDING = StateWording(
    republish_note="повторный запуск опубликует меню в General заново",
    check_hint="проверь закрепы General",
)
UNCERTAIN_ACTIONS = (
    "открой General; если пост меню там есть — удали его в Telegram; "
    "затем удали запись «1» нужного чата из menu_state.json и запусти снова"
)

SYNC_TEXTS = SyncTexts(
    uncertain_hint=f"исход прошлой отправки неизвестен: {UNCERTAIN_ACTIONS}",
    send_unknown_note=f"исход отправки неизвестен: {UNCERTAIN_ACTIONS}",
    edit_unknown_note="исход правки неизвестен — state не менял, проверь General глазами",
    pin_unknown_note="исход закрепа неизвестен — закреп проверь в General, pinned=false",
    marker_release_critical=("Отправка отклонена, но пометку uncertain меню (запись «%s» чата) снять "
                             "не удалось — удали её из menu_state.json вручную"),
    state_lost_critical="Запрос прошёл, но state не сохранён. Запись «%s» чата вручную: %s",
)


class MenuError(Exception):
    """Меню не годится для публикации — ничего не отправлено."""


@dataclass(frozen=True)
class MenuJob:
    """Готовый к отправке пост: всё, что нужно знать для send/edit/pin."""
    html: str
    animation_path: str
    content_hash: str


# ---------------------------------------------------------------------------
# Аргументы и конфиг
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: опечатка вроде --dry не должна молча превращаться в другой флаг.
    parser = argparse.ArgumentParser(prog="menu_bot.py", allow_abbrev=False,
                                     description="Меню-навигатор: публикация и правка закрепа в General")
    parser.add_argument("--dry-run", action="store_true", help="план и rich HTML без сети и без записи state")
    parser.add_argument("--test", action="store_true", help="публиковать в telegram.test_chat_id")
    parser.add_argument("--init", action="store_true", help="разрешить старт без menu_state.json")
    return parser


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Config not found: %s", CONFIG_PATH)
        sys.exit(EXIT_FAILURE)
    except json.JSONDecodeError as e:
        log.error("Config JSON error: %s", e)
        sys.exit(EXIT_FAILURE)


# ---------------------------------------------------------------------------
# Rich HTML
# ---------------------------------------------------------------------------
def build_topic_url(chat: str, thread_id: int) -> str:
    return TOPIC_URL_TEMPLATE.format(chat_name=chat.removeprefix("@"), thread_id=thread_id)


def render_intro(lines: list[str]) -> str:
    return "".join(INTRO_TEMPLATE.format(escape_html(line)) for line in lines)


def render_table_row(section: dict, chat: str) -> str:
    # href экранируется отдельно и с quote=True — см. докстринг rich_render._inline.
    url = html.escape(build_topic_url(chat, section["thread_id"]), quote=True)
    return ROW_TEMPLATE.format(url=url, title=escape_html(section["title"]),
                               description=escape_html(section["description"]))


def render_table(sections: list[dict], chat: str) -> str:
    rows = "".join(render_table_row(section, chat) for section in sections)
    return f"{TABLE_OPEN_TAG}{TABLE_HEADER_ROW}{rows}{TABLE_CLOSE_TAG}"


def render_rules(rules: list[str]) -> str:
    return RULES_TEMPLATE.format(RULES_LINE_BREAK.join(escape_html(rule) for rule in rules))


def render_menu_html(menu: dict) -> str:
    """Пост меню без шапки-анимации: видео-тег добавляет отправка через animation_path."""
    return "".join([
        TITLE_TEMPLATE.format(escape_html(menu["title"])),
        render_intro(menu["intro"]),
        render_table(menu["sections"], menu["chat"]),
        render_rules(menu["rules"]),
    ])


def build_job(menu: dict, menu_dir: Path) -> MenuJob:
    animation_path = resolve_inside(menu_dir, menu["animation"])
    rich_html = render_menu_html(menu)
    return MenuJob(
        html=rich_html,
        animation_path=str(animation_path),
        content_hash=compute_content_hash(rich_html, animation_path.read_bytes()),
    )


# ---------------------------------------------------------------------------
# Меню: чтение и проверка до любой отправки
# ---------------------------------------------------------------------------
def load_menu(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            menu = json.load(f)
    except FileNotFoundError:
        raise MenuError(f"файл меню не найден: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise MenuError(f"меню не читается как JSON: {e}") from None
    if not isinstance(menu, dict):
        raise MenuError("меню должно быть JSON-объектом")
    return menu


def _find_chat_errors(chat) -> list[str]:
    if is_text(chat) and CHAT_NAME_PATTERN.fullmatch(chat):
        return []
    return ["chat: нужно имя группы вида @username (5–32 символа: латиница, цифры, «_»)"]


def _find_title_errors(title) -> list[str]:
    return [] if is_text(title) else ["title: нужна непустая строка"]


def _find_lines_errors(lines, name: str) -> list[str]:
    if not isinstance(lines, list) or not lines:
        return [f"{name}: нужен непустой список строк"]
    return [f"{name}, строка {number}: должна быть непустым текстом"
            for number, line in enumerate(lines, start=1) if not is_text(line)]


def _find_animation_errors(animation, menu_dir: Path) -> list[str]:
    if not is_text(animation):
        return ["animation: нужно имя MP4-файла из папки меню"]
    path = resolve_inside(menu_dir, animation)
    if path is None:
        return [f"animation {animation!r} выходит за папку меню"]
    if path.suffix.lower() != ANIMATION_SUFFIX:
        return [f"animation {animation!r} должен быть {ANIMATION_SUFFIX}-файлом"]
    if not path.is_file():
        return [f"файл анимации не найден: {animation}"]
    return []


def _find_thread_id_errors(thread_id, label: str) -> list[str]:
    if is_int(thread_id) and thread_id >= MIN_SECTION_THREAD_ID:
        return []
    return [f"{label}: thread_id должен быть целым числом не меньше {MIN_SECTION_THREAD_ID}"]


def _find_section_errors(section, index: int) -> list[str]:
    if not isinstance(section, dict):
        return [f"раздел {index}: должен быть объектом"]
    label = f"раздел {index} ({section.get('title')})"
    errors = [f"{label}: нет {name}" for name in SECTION_TEXT_FIELDS if not is_text(section.get(name))]
    return errors + _find_thread_id_errors(section.get("thread_id"), label)


def _find_duplicate_thread_errors(sections: list) -> list[str]:
    ids = [section["thread_id"] for section in sections
           if isinstance(section, dict) and is_int(section.get("thread_id"))]
    duplicates = sorted({thread_id for thread_id in ids if ids.count(thread_id) > 1})
    return [f"thread_id {thread_id} встречается в меню больше одного раза" for thread_id in duplicates]


def _find_sections_errors(sections) -> list[str]:
    if not isinstance(sections, list) or not sections:
        return ["sections: нужен непустой список"]
    errors: list[str] = []
    for index, section in enumerate(sections, start=1):
        errors += _find_section_errors(section, index)
    return errors + _find_duplicate_thread_errors(sections)


def _find_size_errors(menu: dict) -> list[str]:
    total = len(ANIMATION_TAG) + len(render_menu_html(menu))
    if total <= RICH_HTML_MAX_LENGTH:
        return []
    return [f"итоговый html {total} символов — больше лимита rich {RICH_HTML_MAX_LENGTH}"]


def find_menu_errors(menu: dict, menu_dir: Path) -> list[str]:
    """Все проблемы меню разом (пустой список — меню годится). Размер html — только у целого меню."""
    errors = _find_chat_errors(menu.get("chat"))
    errors += _find_title_errors(menu.get("title"))
    errors += _find_lines_errors(menu.get("intro"), "intro")
    errors += _find_lines_errors(menu.get("rules"), "rules")
    errors += _find_animation_errors(menu.get("animation"), menu_dir)
    errors += _find_sections_errors(menu.get("sections"))
    return errors or _find_size_errors(menu)


# ---------------------------------------------------------------------------
# Выполнение
# ---------------------------------------------------------------------------
class MenuSync(PinnedPostSync):
    """Пост меню в General с анимацией-шапкой; движок — в pinned_post_sync."""

    def __init__(self, cfg: dict, store: StateStore, test: bool):
        super().__init__(store, SYNC_TEXTS)
        self.cfg = cfg
        self.test = test

    def state_key(self, job: MenuJob) -> int:
        return GENERAL_THREAD_ID

    def request_send(self, job: MenuJob) -> int | None:
        return send_rich_message(
            self.cfg, job.html, thread_id=None, test=self.test, rubric=None,
            animation_path=job.animation_path,
        )

    def request_edit(self, message_id: int, job: MenuJob) -> int | None:
        return edit_rich_message(
            self.cfg, message_id, job.html, test=self.test, animation_path=job.animation_path,
        )

    def request_pin(self, message_id: int) -> bool:
        return pin_message(self.cfg, message_id, test=self.test)


# ---------------------------------------------------------------------------
# Вывод и запуск
# ---------------------------------------------------------------------------
def resolve_exit_code(outcome: SyncOutcome) -> int:
    if outcome.state_lost:
        return EXIT_STATE_NOT_SAVED
    return EXIT_OK if outcome.ok else EXIT_FAILURE


def run_dry(job: MenuJob, store: StateStore) -> int:
    steps = plan_steps(store.get(GENERAL_THREAD_ID), job.content_hash)
    print(format_plan_line(OUTCOME_LABEL, steps, ANIMATION_CAPTION, job.animation_path))
    print(job.html)
    print("DRY RUN: сеть и state не тронуты")
    return EXIT_OK


def run_live(job: MenuJob, sync: MenuSync) -> int:
    outcome = sync.run(job)
    print(format_outcome_line(OUTCOME_LABEL, outcome), flush=True)
    return resolve_exit_code(outcome)


def prepare_store(cfg: dict, options: argparse.Namespace) -> StateStore:
    """Открывает state группы (или тестового чата); правила --init — в post_state.open_store."""
    chat_key = resolve_chat_key(cfg, options.test)
    return open_store(STATE_PATH, chat_key, options.test, options.init, options.dry_run, STATE_WORDING)


def prepare_job() -> MenuJob:
    menu = load_menu(MENU_PATH)
    errors = find_menu_errors(menu, MENU_DIR)
    if errors:
        raise MenuError("меню не годится:\n  - " + "\n  - ".join(errors))
    return build_job(menu, MENU_DIR)


def execute(cfg: dict, options: argparse.Namespace) -> int:
    job = prepare_job()
    lock_file = acquire_lock(LOCK_PATH)
    if lock_file is None:
        log.info("Другая копия menu_bot уже выполняется (занят %s) — выходим с кодом 0", LOCK_PATH)
        return EXIT_OK
    store = prepare_store(cfg, options)
    if options.dry_run:
        return run_dry(job, store)
    return run_live(job, MenuSync(cfg, store, options.test))


def main(argv: list[str] | None = None) -> int:
    options = build_arg_parser().parse_args(argv)
    try:
        return execute(load_config(), options)
    except (MenuError, StateError, ConfigError) as e:
        log.error("%s", e)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
