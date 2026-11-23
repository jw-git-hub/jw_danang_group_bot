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
import fcntl
import hashlib
import html
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from telegram_sender import (
    SendOutcomeUnknown,
    edit_rich_message,
    pin_message,
    resolve_target,
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

STEP_SEND = "send"
STEP_EDIT = "edit"
STEP_SKIP = "skip"
STEP_PIN = "pin"
STEP_UNCERTAIN = "uncertain"

HASH_SEPARATOR = b"\0"

# Темы 0 и 1 в API уходят как General-чат (без message_thread_id) — для каталога темы это ошибка.
MIN_TOPIC_THREAD_ID = 2

UNCERTAIN_ACTIONS = (
    "открой тему; если пост бота там есть — удали его в Telegram; "
    "затем удали запись темы из catalog_state.json и запусти снова"
)
UNCERTAIN_HINT = f"исход прошлой отправки неизвестен: {UNCERTAIN_ACTIONS}"


class CatalogError(Exception):
    """Каталог не годится для публикации — ничего не отправлено."""


class StateError(Exception):
    """State нельзя прочитать или записать — публикация отменена."""


class ConfigError(Exception):
    """В config.json нет того, что нужно для выбранного режима."""


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


@dataclass(frozen=True)
class SectionOutcome:
    """Итог обработки темы. stop=True — остальные темы трогать нельзя.

    state_lost=True — запрос к Telegram прошёл, а запись о нём в state сохранить не удалось.
    """
    actions: list[str] = field(default_factory=list)
    message_id: int | None = None
    ok: bool = True
    stop: bool = False
    requested: bool = False
    state_lost: bool = False
    note: str = ""


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


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_banner_path(catalog_dir: Path, banner: str) -> Path | None:
    """Путь баннера внутри папки каталога; None, если путь выходит за её пределы."""
    root = catalog_dir.resolve()
    path = (root / banner).resolve()
    return path if path.is_relative_to(root) else None


def _find_chat_errors(catalog: dict, configured_chat_id) -> list[str]:
    # Группа берётся из config.json, а chat_id каталога — только защита от чужого
    # файла. В конфиге id бывает строкой, в каталоге числом — сравниваем как строки.
    if "chat_id" not in catalog or str(catalog["chat_id"]) == str(configured_chat_id):
        return []
    return ["chat_id каталога не совпадает с группой из config.json — каталог чужой"]


def _find_banner_errors(banner, label: str, catalog_dir: Path) -> list[str]:
    if not _is_text(banner):
        return [f"{label}: нет banner"]
    path = resolve_banner_path(catalog_dir, banner)
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
    if not _is_text(url):
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
    errors = [f"{label}: нет {name}" for name in ITEM_TEXT_FIELDS if not _is_text(item.get(name))]
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
    if _is_int(thread_id) and thread_id >= MIN_TOPIC_THREAD_ID:
        return []
    return [f"{label}: message_thread_id должен быть целым числом не меньше {MIN_TOPIC_THREAD_ID} "
            f"(0 и 1 уходят в General)"]


def _find_section_errors(section, index: int, catalog_dir: Path) -> list[str]:
    if not isinstance(section, dict):
        return [f"секция {index}: должна быть объектом"]
    label = f"секция {index} ({section.get('title')})"
    errors = [f"{label}: нет {name}" for name in ("title", "lead") if not _is_text(section.get(name))]
    errors += _find_thread_id_errors(section, label)
    errors += _find_banner_errors(section.get("banner"), label, catalog_dir)
    return errors + _find_items_errors(section.get("items"), label)


def _find_duplicate_thread_errors(sections: list) -> list[str]:
    seen: set[int] = set()
    errors = []
    for section in sections:
        thread_id = section.get("message_thread_id") if isinstance(section, dict) else None
        if _is_int(thread_id) and thread_id in seen:
            errors.append(f"message_thread_id {thread_id} встречается в каталоге больше одного раза")
        seen.add(thread_id)
    return errors


def find_catalog_errors(catalog: dict, configured_chat_id, catalog_dir: Path) -> list[str]:
    """Все проблемы каталога разом (пустой список — каталог годится)."""
    errors = _find_chat_errors(catalog, configured_chat_id)
    if not _is_text(catalog.get("footer")):
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


def compute_content_hash(rich_html: str, banner_bytes: bytes) -> str:
    """Отпечаток поста: меняется при правке текста/ссылок и при замене баннера."""
    digest = hashlib.sha256()
    digest.update(rich_html.encode("utf-8"))
    digest.update(HASH_SEPARATOR)
    digest.update(banner_bytes)
    return digest.hexdigest()


def build_job(section: dict, footer: str, catalog_dir: Path) -> SectionJob:
    banner_path = resolve_banner_path(catalog_dir, section["banner"])
    rich_html = render_section_html(section, footer)
    return SectionJob(
        thread_id=section["message_thread_id"],
        title=section["title"],
        html=rich_html,
        banner_path=str(banner_path),
        content_hash=compute_content_hash(rich_html, banner_path.read_bytes()),
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_entry(message_id: int, pinned: bool, content_hash: str) -> dict:
    return {"message_id": message_id, "pinned": pinned,
            "content_hash": content_hash, "updated_at": _now_iso()}


def make_uncertain_entry() -> dict:
    """Пометка «запрос ушёл или мог уйти»: пишется на диск ДО отправки."""
    return {"uncertain": True, "updated_at": _now_iso()}


def _is_valid_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("uncertain") is True:
        return True
    return _is_int(entry.get("message_id")) and isinstance(entry.get("content_hash"), str)


def find_state_shape_error(data) -> str | None:
    """Ожидается {chat_id: {thread_id: запись}}; None — форма верная."""
    if not isinstance(data, dict):
        return "state должен быть JSON-объектом"
    for chat_key, threads in data.items():
        if not isinstance(threads, dict):
            return f"state[{chat_key}] должен быть объектом"
        for thread_key, entry in threads.items():
            if not _is_valid_entry(entry):
                return f"state[{chat_key}][{thread_key}]: запись без message_id/content_hash"
    return None


def quarantine_corrupt_state(path: Path, reason: str) -> None:
    """Битый state не теряем и не затираем: переименовываем в сторону для разбора."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, corrupt_path)
        log.error("State повреждён (%s): переименован в %s, нужен ручной разбор", reason, corrupt_path)
    except OSError as e:
        log.error("State повреждён (%s) и не переименован в .corrupt: %s", reason, e)


def _missing_state_error(path: Path) -> StateError:
    quarantined = sorted(p.name for p in path.parent.glob(f"{path.name}.corrupt-*"))
    hint = f" (есть карантинные копии: {', '.join(quarantined)})" if quarantined else ""
    return StateError(
        f"нет {path.name}{hint}: если state потерян, повторный запуск опубликует каталог заново "
        f"во все темы — проверь группу и, только если так и нужно, передай --init"
    )


def load_state(path: Path, init: bool, dry_run: bool) -> dict:
    """Читает state. Нет файла: dry-run и --init стартуют с пустого, иначе StateError.

    В dry-run ничего не пишем на диск, в том числе не переименовываем битый файл.
    """
    if not path.exists():
        if init or dry_run:
            return {}
        raise _missing_state_error(path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        shape_error = find_state_shape_error(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        data, shape_error = None, f"не читается как JSON: {e}"
    if shape_error:
        if not dry_run:
            quarantine_corrupt_state(path, shape_error)
        raise StateError(f"{path.name} повреждён: {shape_error}")
    return data


def save_state(data: dict, path: Path) -> bool:
    """Атомарно пишет state (mkstemp рядом + os.replace). False — запись не удалась."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.error("Не удалось сохранить state в %s: %s", path, e)
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        return False


class StateStore:
    """state группы (или тестового чата): запись темы сохраняется на диск сразу."""

    def __init__(self, path: Path, data: dict, chat_key: str):
        self.path = path
        self.data = data
        self.chat_key = chat_key

    def get(self, thread_id: int) -> dict | None:
        return self.data.get(self.chat_key, {}).get(str(thread_id))

    def put(self, thread_id: int, entry: dict) -> bool:
        self.data.setdefault(self.chat_key, {})[str(thread_id)] = entry
        return save_state(self.data, self.path)

    def remove(self, thread_id: int) -> bool:
        self.data.get(self.chat_key, {}).pop(str(thread_id), None)
        return save_state(self.data, self.path)


def acquire_lock(lock_path: Path):
    """Неблокирующий flock; None — занят другим запуском. Держится до конца процесса."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


# ---------------------------------------------------------------------------
# План и выполнение по теме
# ---------------------------------------------------------------------------
def plan_steps(entry: dict | None, content_hash: str) -> list[str]:
    """Что делать с темой по текущему state. Один источник правды для dry-run и боевого запуска."""
    if entry is None:
        return [STEP_SEND, STEP_PIN]
    if entry.get("uncertain"):
        return [STEP_UNCERTAIN]
    steps = [STEP_EDIT if entry["content_hash"] != content_hash else STEP_SKIP]
    if not entry.get("pinned"):
        steps.append(STEP_PIN)
    return steps


class SectionSync:
    """Приводит одну тему к актуальному состоянию: отправка или правка, затем закреп."""

    def __init__(self, cfg: dict, store: StateStore, test: bool):
        self.cfg = cfg
        self.store = store
        self.test = test

    def run(self, job: SectionJob) -> SectionOutcome:
        entry = self.store.get(job.thread_id)
        steps = plan_steps(entry, job.content_hash)
        if steps[0] == STEP_UNCERTAIN:
            return self._skip_uncertain()
        outcome = {STEP_SEND: self._send, STEP_EDIT: self._edit, STEP_SKIP: self._keep}[steps[0]](job, entry)
        if outcome.ok and STEP_PIN in steps:
            outcome = self._pin(job, outcome)
        return outcome

    def _skip_uncertain(self) -> SectionOutcome:
        return SectionOutcome(actions=[STEP_UNCERTAIN], ok=False, note=UNCERTAIN_HINT)

    def _keep(self, job: SectionJob, entry: dict) -> SectionOutcome:
        return SectionOutcome(actions=[STEP_SKIP], message_id=entry["message_id"], note="без изменений")

    def _send(self, job: SectionJob, entry: None) -> SectionOutcome:
        # Пометка идёт на диск ДО запроса: если процесс убьют (Ctrl+C, kill) посреди отправки,
        # следующий запуск увидит uncertain и не пошлёт пост второй раз.
        if not self.store.put(job.thread_id, make_uncertain_entry()):
            return SectionOutcome(actions=[STEP_SEND], ok=False, stop=True,
                                  note="не удалось записать state до отправки — ничего не отправлено")
        try:
            message_id = send_rich_message(
                self.cfg, job.html, thread_id=job.thread_id, test=self.test, rubric=None,
                photo_path=job.banner_path, photo_mime=BANNER_MIME,
            )
        except SendOutcomeUnknown:
            return SectionOutcome(actions=[STEP_SEND], ok=False, stop=True, requested=True,
                                  note=f"исход отправки неизвестен: {UNCERTAIN_ACTIONS}. Остальные темы не тронуты")
        if message_id is None:
            return self._release_marker(job)
        # Настоящая запись заменяет пометку сразу: если упадёт закреп, повторной отправки не будет.
        return self._persist(job, make_entry(message_id, False, job.content_hash), [STEP_SEND])

    def _release_marker(self, job: SectionJob) -> SectionOutcome:
        """Точный отказ Telegram: пост не создан, пометку uncertain снимаем — тема снова «чистая»."""
        note = "Telegram отклонил отправку"
        if self.store.remove(job.thread_id):
            return SectionOutcome(actions=[STEP_SEND], ok=False, requested=True, note=note)
        log.critical("Отправка отклонена, но пометку uncertain темы %s снять не удалось — "
                     "удали запись темы из catalog_state.json вручную", job.thread_id)
        return SectionOutcome(actions=[STEP_SEND], ok=False, stop=True, requested=True,
                              note=f"{note}, но пометку в state снять не удалось (CRITICAL в логе)")

    def _edit(self, job: SectionJob, entry: dict) -> SectionOutcome:
        message_id = entry["message_id"]
        try:
            edited_id = edit_rich_message(
                self.cfg, message_id, job.html, test=self.test,
                photo_path=job.banner_path, photo_mime=BANNER_MIME,
            )
        except SendOutcomeUnknown:
            return SectionOutcome(actions=[STEP_EDIT], message_id=message_id, ok=False, stop=True,
                                  requested=True, note="исход правки неизвестен — state не менял, проверь тему глазами")
        if edited_id is None:
            return SectionOutcome(actions=[STEP_EDIT], message_id=message_id, ok=False, requested=True,
                                  note="Telegram отклонил правку (пост удалён или уже такой же — причина в ERROR выше)")
        updated = make_entry(message_id, bool(entry.get("pinned")), job.content_hash)
        return self._persist(job, updated, [STEP_EDIT])

    def _persist(self, job: SectionJob, entry: dict, actions: list[str]) -> SectionOutcome:
        """Сохраняет запись темы после успешного запроса; без state следующий запуск продублирует пост."""
        if self.store.put(job.thread_id, entry):
            return SectionOutcome(actions=actions, message_id=entry["message_id"], requested=True)
        log.critical("Запрос прошёл, но state не сохранён. Запись для темы %s вручную: %s",
                     job.thread_id, json.dumps(entry, ensure_ascii=False))
        return SectionOutcome(actions=actions, message_id=entry["message_id"], ok=False, stop=True,
                              requested=True, state_lost=True,
                              note="state не сохранён — запись в логе (CRITICAL)")

    def _pin(self, job: SectionJob, outcome: SectionOutcome) -> SectionOutcome:
        actions = outcome.actions + [STEP_PIN]
        try:
            pinned = pin_message(self.cfg, outcome.message_id, test=self.test)
        except SendOutcomeUnknown:
            return replace(outcome, actions=actions, ok=False, requested=True,
                           note="исход закрепа неизвестен — закреп проверь в теме, pinned=false")
        if not pinned:
            return replace(outcome, actions=actions, ok=False, requested=True,
                           note="закрепить не удалось (нужно право закреплять у бота) — pinned=false")
        entry = {**self.store.get(job.thread_id), "pinned": True, "updated_at": _now_iso()}
        return self._persist(job, entry, actions)


# ---------------------------------------------------------------------------
# Вывод и запуск
# ---------------------------------------------------------------------------
def format_outcome_line(job: SectionJob, outcome: SectionOutcome) -> str:
    message = f" message_id={outcome.message_id}" if outcome.message_id else ""
    status = "" if outcome.ok else "СБОЙ "
    note = f" — {outcome.note}" if outcome.note else ""
    return f"[{job.thread_id}] {job.title}: {status}{' + '.join(outcome.actions)}{message}{note}"


def print_summary(outcomes: list[SectionOutcome], total: int) -> int:
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
        print(f"[{job.thread_id}] {job.title}: план {' + '.join(steps)}; баннер {job.banner_path}")
        print(job.html)
    print(f"DRY RUN: тем {len(jobs)}, сеть и state не тронуты")
    return EXIT_OK


def run_live(jobs: list[SectionJob], sync: SectionSync) -> int:
    outcomes: list[SectionOutcome] = []
    for job in jobs:
        if outcomes and outcomes[-1].requested:
            time.sleep(PAUSE_BETWEEN_SECTIONS_SECONDS)
        outcome = sync.run(job)
        outcomes.append(outcome)
        print(format_outcome_line(job, outcome), flush=True)
        if outcome.stop:
            log.error("Остановка: остальные темы не обрабатываются, пока не разобран этот сбой")
            break
    return print_summary(outcomes, len(jobs))


def resolve_chat_key(cfg: dict, test: bool) -> str:
    """Ключ state: id группы либо id тестового чата — их посты живут в state раздельно."""
    try:
        chat_id, _thread = resolve_target(cfg, None, test)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    return str(chat_id)


def _missing_chat_error(chat_key: str, test: bool) -> StateError:
    target = "тестового чата" if test else "группы"
    return StateError(
        f"в {STATE_PATH.name} нет записей для {target} ({chat_key}): если это первый запуск для него — "
        f"передай --init; если state потерян или подменён, проверь темы чата, иначе повторный запуск "
        f"опубликует каталог заново во все темы"
    )


def prepare_store(cfg: dict, options: argparse.Namespace) -> StateStore:
    """Открывает state группы (или тестового чата). В боевом режиме проверяет, что на диск можно писать.

    --init нужен не только при отсутствии файла, но и когда в нём нет ключа целевого чата:
    state тестового чата не должен молча разрешать первую боевую публикацию.
    """
    chat_key = resolve_chat_key(cfg, options.test)
    data = load_state(STATE_PATH, init=options.init, dry_run=options.dry_run)
    if not (options.init or options.dry_run) and chat_key not in data:
        raise _missing_chat_error(chat_key, options.test)
    store = StateStore(STATE_PATH, data, chat_key)
    if not options.dry_run and not save_state(data, STATE_PATH):
        raise StateError(f"в {STATE_PATH.name} нельзя писать — отправка отменена")
    return store


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
