#!/usr/bin/env python3
"""Каталог исполнителей → закреп в каждой теме группы.

Ручная команда, не cron. Для каждой темы из catalog/catalog.json публикует
rich-пост (шапка-картинка, заголовок, вводная строка, таблица «Имя | Услуга»,
подвал) и закрепляет его без уведомления. При повторном запуске пост не
дублируется: если каталог изменился — правится тот же message_id, если нет —
ничего не отправляется.

Запуск: python3 catalog_bot.py [--dry-run] [--test] [--init] [--thread N[,N...]]
  --dry-run — без сети и без записи state: показать план (send/edit/skip/pin) и rich HTML.
  --test    — публиковать в telegram.test_chat_id; state ведётся отдельно, под его id.
  --init    — разрешить старт без catalog_state.json и для чата, которого в state ещё нет
              (первая публикация каталога в группу или в тестовый чат).
  --thread  — только указанные темы (message_thread_id через запятую).
Коды выхода: 0 — успех / без изменений / лок занят другим запуском;
1 — сбой, исход отправки неизвестен, каталог или state не годятся;
2 — ошибка аргументов, а также «опубликовано, но state не сохранён» (как у vietnamese_bot):
    пост в Telegram уже есть, запись о нём в state нужно внести вручную из лога (CRITICAL).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pinned_post_sync import (
    PinnedPostSync,
    SyncOutcome,
    SyncTexts,
    format_outcome_line,
    format_plan_line,
)
from post_state import (
    STEP_SKIP,
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
    edit_rich_message,
    pin_message,
    send_rich_message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("catalog_bot")

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CATALOG_PATH = BASE_DIR / "catalog" / "catalog.json"
STATE_PATH = BASE_DIR / "catalog_state.json"
LOCK_PATH = STATE_PATH.with_name(STATE_PATH.name + ".lock")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_STATE_NOT_SAVED = 2

BANNER_MIME = "image/png"
BANNER_SUFFIX = ".png"
BANNER_CAPTION = "баннер"
HTTPS_PREFIX = "https://"
ITEM_TEXT_FIELDS = ("name", "service", "url")

# Лимит Telegram — около 20 сообщений в минуту на группу; 16 тем с запасом
# укладываются, если между секциями с запросами выдерживать эту паузу.
PAUSE_BETWEEN_SECTIONS_SECONDS = 4

# Оформление таблицы обычных исполнителей меняется только здесь.
TABLE_OPEN_TAG = "<table bordered striped>"
TABLE_HEADER_NAME = "Имя"
TABLE_HEADER_SERVICE = "Услуга"

# Выделенные исполнители (item.featured: true) выводятся блоком-цитатой над таблицей и в неё не попадают.
FEATURED_FIELD = "featured"
FEATURED_MARK = "⭐"
FEATURED_CAPTION = "Рекомендуем"
FEATURED_LINE_BREAK = "<br/>"
FEATURED_SERVICE_SEPARATOR = " — "

STATE_WORDING = StateWording(
    republish_note="повторный запуск опубликует каталог заново во все темы",
    check_hint="проверь темы чата",
)

# Темы 0 и 1 в API уходят как General-чат (без message_thread_id) — для каталога темы это ошибка.
MIN_TOPIC_THREAD_ID = 2

UNCERTAIN_ACTIONS = (
    "открой тему; если пост бота там есть — удали его в Telegram; "
    "затем удали запись темы из catalog_state.json и запусти снова"
)
UNCERTAIN_HINT = f"исход прошлой отправки неизвестен: {UNCERTAIN_ACTIONS}"

SYNC_TEXTS = SyncTexts(
    uncertain_hint=UNCERTAIN_HINT,
    send_unknown_note=f"исход отправки неизвестен: {UNCERTAIN_ACTIONS}. Остальные темы не тронуты",
    edit_unknown_note="исход правки неизвестен — state не менял, проверь тему глазами",
    pin_unknown_note="исход закрепа неизвестен — закреп проверь в теме, pinned=false",
    marker_release_critical=("Отправка отклонена, но пометку uncertain темы %s снять не удалось — "
                             "удали запись темы из catalog_state.json вручную"),
    state_lost_critical="Запрос прошёл, но state не сохранён. Запись для темы %s вручную: %s",
)


class CatalogError(Exception):
    """Каталог не годится для публикации — ничего не отправлено."""


class UsageError(Exception):
    """Аргументы не согласуются с каталогом (например, неизвестная тема)."""


@dataclass(frozen=True)
class SectionJob:
    """Готовая к отправке тема: всё, что нужно знать для send/edit/pin."""
    thread_id: int
    title: str
    html: str
    banner_path: str
    content_hash: str


# ---------------------------------------------------------------------------
# Аргументы и конфиг
# ---------------------------------------------------------------------------
def parse_thread_ids(raw: str) -> list[int]:
    """«60,68» -> [60, 68]. Всё, что не список чисел через запятую, — ошибка аргумента."""
    parts = [part.strip() for part in raw.split(",")]
    if not all(part.isascii() and part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(f"нужен список id тем через запятую, получено: {raw!r}")
    return [int(part) for part in parts]


def build_arg_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: опечатка вроде --dry не должна молча превращаться в другой флаг.
    parser = argparse.ArgumentParser(prog="catalog_bot.py", allow_abbrev=False,
                                     description="Каталог исполнителей: публикация и правка закрепов в темах")
    parser.add_argument("--dry-run", action="store_true", help="план и rich HTML без сети и без записи state")
    parser.add_argument("--test", action="store_true", help="публиковать в telegram.test_chat_id")
    parser.add_argument("--init", action="store_true", help="разрешить старт без catalog_state.json")
    parser.add_argument("--thread", action="append", type=parse_thread_ids, metavar="N[,N...]",
                        help="только указанные темы (message_thread_id)")
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
# Каталог: чтение и проверка до любой отправки
# ---------------------------------------------------------------------------
def load_catalog(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            catalog = json.load(f)
    except FileNotFoundError:
        raise CatalogError(f"файл каталога не найден: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise CatalogError(f"каталог не читается как JSON: {e}") from None
    if not isinstance(catalog, dict):
        raise CatalogError("каталог должен быть JSON-объектом")
    return catalog


def _find_chat_errors(catalog: dict, configured_chat_id) -> list[str]:
    # Группа берётся из config.json, а chat_id каталога — только защита от чужого
    # файла. В конфиге id бывает строкой, в каталоге числом — сравниваем как строки.
    if "chat_id" not in catalog or str(catalog["chat_id"]) == str(configured_chat_id):
        return []
    return ["chat_id каталога не совпадает с группой из config.json — каталог чужой"]


def _find_banner_errors(banner, label: str, catalog_dir: Path) -> list[str]:
    if not is_text(banner):
        return [f"{label}: нет banner"]
    path = resolve_inside(catalog_dir, banner)
    if path is None:
        return [f"{label}: banner {banner!r} выходит за папку каталога"]
    if path.suffix.lower() != BANNER_SUFFIX:
        return [f"{label}: banner {banner!r} должен быть {BANNER_SUFFIX}-файлом"]
    if not path.is_file():
        return [f"{label}: файл баннера не найден: {banner}"]
    return []


def _find_featured_errors(item: dict, label: str) -> list[str]:
    if FEATURED_FIELD in item and not isinstance(item[FEATURED_FIELD], bool):
        return [f"{label}: {FEATURED_FIELD} должен быть true или false"]
    return []


def _has_host(url: str) -> bool:
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _find_url_errors(url, label: str) -> list[str]:
    if not is_text(url):
        return []
    errors = []
    if not url.startswith(HTTPS_PREFIX):
        errors.append(f"{label}: url должен начинаться с {HTTPS_PREFIX}")
    if not url.isprintable() or any(char.isspace() for char in url):
        errors.append(f"{label}: в url есть пробельные или невидимые символы")
    if not _has_host(url):
        errors.append(f"{label}: в url нет хоста")
    return errors


def _find_item_errors(item, label: str) -> list[str]:
    if not isinstance(item, dict):
        return [f"{label}: исполнитель должен быть объектом"]
    errors = [f"{label}: нет {name}" for name in ITEM_TEXT_FIELDS if not is_text(item.get(name))]
    errors += _find_featured_errors(item, label)
    return errors + _find_url_errors(item.get("url"), label)


def _find_items_errors(items, label: str) -> list[str]:
    if not isinstance(items, list) or not items:
        return [f"{label}: items пуст или не список"]
    errors: list[str] = []
    for number, item in enumerate(items, start=1):
        errors += _find_item_errors(item, f"{label}, исполнитель {number}")
    return errors


def _find_thread_id_errors(section: dict, label: str) -> list[str]:
    thread_id = section.get("message_thread_id")
    if is_int(thread_id) and thread_id >= MIN_TOPIC_THREAD_ID:
        return []
    return [f"{label}: message_thread_id должен быть целым числом не меньше {MIN_TOPIC_THREAD_ID} "
            f"(0 и 1 уходят в General)"]


def _find_section_errors(section, index: int, catalog_dir: Path) -> list[str]:
    if not isinstance(section, dict):
        return [f"секция {index}: должна быть объектом"]
    label = f"секция {index} ({section.get('title')})"
    errors = [f"{label}: нет {name}" for name in ("title", "lead") if not is_text(section.get(name))]
    errors += _find_thread_id_errors(section, label)
    errors += _find_banner_errors(section.get("banner"), label, catalog_dir)
    return errors + _find_items_errors(section.get("items"), label)


def _find_duplicate_thread_errors(sections: list) -> list[str]:
    seen: set[int] = set()
    errors = []
    for section in sections:
        thread_id = section.get("message_thread_id") if isinstance(section, dict) else None
        if is_int(thread_id) and thread_id in seen:
            errors.append(f"message_thread_id {thread_id} встречается в каталоге больше одного раза")
        seen.add(thread_id)
    return errors


def find_catalog_errors(catalog: dict, configured_chat_id, catalog_dir: Path) -> list[str]:
    """Все проблемы каталога разом (пустой список — каталог годится)."""
    errors = _find_chat_errors(catalog, configured_chat_id)
    if not is_text(catalog.get("footer")):
        errors.append("footer: нужна непустая строка")
    sections = catalog.get("sections")
    if not isinstance(sections, list) or not sections:
        return errors + ["sections: нужен непустой список"]
    for index, section in enumerate(sections, start=1):
        errors += _find_section_errors(section, index, catalog_dir)
    return errors + _find_duplicate_thread_errors(sections)


def select_sections(catalog: dict, thread_groups: list[list[int]] | None) -> list[dict]:
    """Секции в порядке каталога; --thread сужает выбор, неизвестная тема — UsageError."""
    sections = catalog["sections"]
    if not thread_groups:
        return sections
    wanted = {thread_id for group in thread_groups for thread_id in group}
    unknown = wanted - {section["message_thread_id"] for section in sections}
    if unknown:
        raise UsageError(f"в каталоге нет тем: {', '.join(str(t) for t in sorted(unknown))}")
    return [section for section in sections if section["message_thread_id"] in wanted]


# ---------------------------------------------------------------------------
# Rich HTML
# ---------------------------------------------------------------------------
def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def is_featured(item: dict) -> bool:
    return item.get(FEATURED_FIELD) is True


def split_featured(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(выделенные, обычные); внутри каждой группы порядок каталога."""
    featured = [item for item in items if is_featured(item)]
    regular = [item for item in items if not is_featured(item)]
    return featured, regular


def render_name_link(item: dict) -> str:
    # href экранируется отдельно и с quote=True — см. докстринг rich_render._inline.
    href = html.escape(item["url"], quote=True)
    return f'<a href="{href}">{_escape(item["name"])}</a>'


def render_featured_line(item: dict) -> str:
    return f"<b>{render_name_link(item)}</b>{FEATURED_SERVICE_SEPARATOR}{_escape(item['service'])}"


def render_featured_block(featured: list[dict]) -> str:
    """Блок-цитата «⭐ Рекомендуем» над таблицей; без выделенных — пустая строка."""
    if not featured:
        return ""
    lines = [f"{FEATURED_MARK} <b>{FEATURED_CAPTION}</b>"] + [render_featured_line(item) for item in featured]
    return f"<blockquote>{FEATURED_LINE_BREAK.join(lines)}</blockquote>"


def render_table_row(item: dict) -> str:
    return f"<tr><td>{render_name_link(item)}</td><td>{_escape(item['service'])}</td></tr>"


def render_table(items: list[dict]) -> str:
    """Таблица «Имя | Услуга»; без обычных исполнителей таблицы нет вовсе."""
    if not items:
        return ""
    header = f"<tr><th>{TABLE_HEADER_NAME}</th><th>{TABLE_HEADER_SERVICE}</th></tr>"
    rows = "".join(render_table_row(item) for item in items)
    return f"{TABLE_OPEN_TAG}{header}{rows}</table>"


def render_section_html(section: dict, footer: str) -> str:
    """Пост темы без шапки-картинки: её добавляет отправка через photo_path."""
    featured, regular = split_featured(section["items"])
    return "".join([
        f"<h3>{_escape(section['title'])}</h3>",
        f"<p>{_escape(section['lead'])}</p>",
        render_featured_block(featured),
        render_table(regular),
        f"<footer>{_escape(footer)}</footer>",
    ])


def build_job(section: dict, footer: str, catalog_dir: Path) -> SectionJob:
    banner_path = resolve_inside(catalog_dir, section["banner"])
    rich_html = render_section_html(section, footer)
    return SectionJob(
        thread_id=section["message_thread_id"],
        title=section["title"],
        html=rich_html,
        banner_path=str(banner_path),
        content_hash=compute_content_hash(rich_html, banner_path.read_bytes()),
    )


# ---------------------------------------------------------------------------
# Выполнение по теме
# ---------------------------------------------------------------------------
class SectionSync(PinnedPostSync):
    """Тема каталога: пост с баннером-картинкой в теме job.thread_id; движок — в pinned_post_sync."""

    def __init__(self, cfg: dict, store: StateStore, test: bool):
        super().__init__(store, SYNC_TEXTS)
        self.cfg = cfg
        self.test = test

    def state_key(self, job: SectionJob) -> int:
        return job.thread_id

    def request_send(self, job: SectionJob) -> int | None:
        return send_rich_message(
            self.cfg, job.html, thread_id=job.thread_id, test=self.test, rubric=None,
            photo_path=job.banner_path, photo_mime=BANNER_MIME,
        )

    def request_edit(self, message_id: int, job: SectionJob) -> int | None:
        return edit_rich_message(
            self.cfg, message_id, job.html, test=self.test,
            photo_path=job.banner_path, photo_mime=BANNER_MIME,
        )

    def request_pin(self, message_id: int) -> bool:
        return pin_message(self.cfg, message_id, test=self.test)


# ---------------------------------------------------------------------------
# Вывод и запуск
# ---------------------------------------------------------------------------
def section_label(job: SectionJob) -> str:
    return f"[{job.thread_id}] {job.title}"


def print_summary(outcomes: list[SyncOutcome], total: int) -> int:
    failed = sum(1 for outcome in outcomes if not outcome.ok)
    unchanged = sum(1 for outcome in outcomes if outcome.ok and outcome.actions == [STEP_SKIP])
    skipped = total - len(outcomes)
    print(f"Итог: тем {total}, обработано {len(outcomes)}, без изменений {unchanged}, "
          f"сбоев {failed}, не обработано {skipped}")
    if any(outcome.state_lost for outcome in outcomes):
        return EXIT_STATE_NOT_SAVED
    return EXIT_FAILURE if failed or skipped else EXIT_OK


def run_dry(jobs: list[SectionJob], store: StateStore) -> int:
    for job in jobs:
        steps = plan_steps(store.get(job.thread_id), job.content_hash)
        print(format_plan_line(section_label(job), steps, BANNER_CAPTION, job.banner_path))
        print(job.html)
    print(f"DRY RUN: тем {len(jobs)}, сеть и state не тронуты")
    return EXIT_OK


def run_live(jobs: list[SectionJob], sync: SectionSync) -> int:
    outcomes: list[SyncOutcome] = []
    for job in jobs:
        if outcomes and outcomes[-1].requested:
            time.sleep(PAUSE_BETWEEN_SECTIONS_SECONDS)
        outcome = sync.run(job)
        outcomes.append(outcome)
        print(format_outcome_line(section_label(job), outcome), flush=True)
        if outcome.stop:
            log.error("Остановка: остальные темы не обрабатываются, пока не разобран этот сбой")
            break
    return print_summary(outcomes, len(jobs))


def prepare_store(cfg: dict, options: argparse.Namespace) -> StateStore:
    """Открывает state группы (или тестового чата); правила --init — в post_state.open_store."""
    chat_key = resolve_chat_key(cfg, options.test)
    return open_store(STATE_PATH, chat_key, options.test, options.init, options.dry_run, STATE_WORDING)


def prepare_jobs(cfg: dict, options: argparse.Namespace) -> list[SectionJob]:
    catalog = load_catalog(CATALOG_PATH)
    errors = find_catalog_errors(catalog, cfg["telegram"]["chat_id"], CATALOG_PATH.parent)
    if errors:
        raise CatalogError("каталог не годится:\n  - " + "\n  - ".join(errors))
    sections = select_sections(catalog, options.thread)
    return [build_job(section, catalog["footer"], CATALOG_PATH.parent) for section in sections]


def execute(cfg: dict, options: argparse.Namespace) -> int:
    jobs = prepare_jobs(cfg, options)
    lock_file = acquire_lock(LOCK_PATH)
    if lock_file is None:
        log.info("Другая копия catalog_bot уже выполняется (занят %s) — выходим с кодом 0", LOCK_PATH)
        return EXIT_OK
    store = prepare_store(cfg, options)
    if options.dry_run:
        return run_dry(jobs, store)
    return run_live(jobs, SectionSync(cfg, store, options.test))


def main(argv: list[str] | None = None) -> int:
    options = build_arg_parser().parse_args(argv)
    try:
        return execute(load_config(), options)
    except UsageError as e:
        log.error("%s", e)
        return EXIT_USAGE
    except (CatalogError, StateError, ConfigError) as e:
        log.error("%s", e)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
