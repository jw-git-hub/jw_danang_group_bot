"""State закреплённых постов: общая основа ручных ботов catalog_bot и menu_bot.

Хранит для каждого чата (группа или тестовый чат — под разными ключами) запись
о каждом посте: message_id, закреплён ли он, хеш содержимого. Форма файла:
{chat_id: {ключ_поста: запись}}, где ключ — thread_id темы (у меню это 1, General).

Что делает модуль:
  - запись state атомарная (mkstemp рядом + os.replace), запись поста сохраняется сразу;
  - форма файла проверяется при чтении; битый файл не затирается, а переименовывается
    в <файл>.corrupt-<время> для разбора;
  - пометка uncertain («запрос ушёл или мог уйти») — вид записи, которую пишет движок
    pinned_post_sync до отправки;
  - open_store открывает state группы или тестового чата: --init нужен, пока файла или
    записей целевого чата нет;
  - flock не даёт двум запускам работать одновременно;
  - compute_content_hash — отпечаток поста (разметка + байты картинки/анимации): по нему
    видно, что пост изменился и его нужно править;
  - plan_steps — единственный источник правды о том, что делать с постом
    (send / edit / skip / pin / uncertain), для dry-run и боевого запуска.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telegram_sender import resolve_target

log = logging.getLogger("post_state")

STEP_SEND = "send"
STEP_EDIT = "edit"
STEP_SKIP = "skip"
STEP_PIN = "pin"
STEP_UNCERTAIN = "uncertain"

HASH_SEPARATOR = b"\0"

class StateError(Exception):
    """State нельзя прочитать или записать — публикация отменена."""


class ConfigError(Exception):
    """В config.json нет того, что нужно для выбранного режима."""


@dataclass(frozen=True)
class StateWording:
    """Что бот пишет владельцу, когда state нет: чем грозит потеря записи и что проверить."""
    republish_note: str
    check_hint: str


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def resolve_inside(root_dir: Path, relative: str) -> Path | None:
    """Путь внутри папки данных; None, если он выходит за её пределы (../, абсолютный путь)."""
    root = root_dir.resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_content_hash(rich_html: str, media_bytes: bytes) -> str:
    """Отпечаток поста: меняется при правке текста/ссылок и при замене картинки или анимации."""
    digest = hashlib.sha256()
    digest.update(rich_html.encode("utf-8"))
    digest.update(HASH_SEPARATOR)
    digest.update(media_bytes)
    return digest.hexdigest()


def make_entry(message_id: int, pinned: bool, content_hash: str) -> dict:
    return {"message_id": message_id, "pinned": pinned,
            "content_hash": content_hash, "updated_at": now_iso()}


def make_uncertain_entry() -> dict:
    """Пометка «запрос ушёл или мог уйти»: пишется на диск ДО отправки."""
    return {"uncertain": True, "updated_at": now_iso()}


def _is_valid_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("uncertain") is True:
        return True
    return is_int(entry.get("message_id")) and isinstance(entry.get("content_hash"), str)


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


def _missing_state_error(path: Path, republish_note: str) -> StateError:
    quarantined = sorted(p.name for p in path.parent.glob(f"{path.name}.corrupt-*"))
    hint = f" (есть карантинные копии: {', '.join(quarantined)})" if quarantined else ""
    return StateError(
        f"нет {path.name}{hint}: если state потерян, {republish_note} "
        f"— проверь группу и, только если так и нужно, передай --init"
    )


def load_state(path: Path, init: bool, dry_run: bool, republish_note: str) -> dict:
    """Читает state. Нет файла: dry-run и --init стартуют с пустого, иначе StateError.

    republish_note — что случится, если state потерян (попадает в текст ошибки).
    В dry-run ничего не пишем на диск, в том числе не переименовываем битый файл.
    """
    if not path.exists():
        if init or dry_run:
            return {}
        raise _missing_state_error(path, republish_note)
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


def resolve_chat_key(cfg: dict, test: bool) -> str:
    """Ключ state: id группы либо id тестового чата — их посты живут в state раздельно."""
    try:
        chat_id, _thread = resolve_target(cfg, None, test)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    return str(chat_id)


def missing_chat_error(path: Path, chat_key: str, test: bool, wording: StateWording) -> StateError:
    target = "тестового чата" if test else "группы"
    return StateError(
        f"в {path.name} нет записей для {target} ({chat_key}): если это первый запуск для него — "
        f"передай --init; если state потерян или подменён, {wording.check_hint}, иначе {wording.republish_note}"
    )


def open_store(path: Path, chat_key: str, test: bool, init: bool, dry_run: bool,
               wording: StateWording) -> StateStore:
    """Открывает state группы (или тестового чата). В боевом режиме проверяет, что на диск можно писать.

    --init нужен не только при отсутствии файла, но и когда в нём нет ключа целевого чата:
    state тестового чата не должен молча разрешать первую боевую публикацию.
    """
    data = load_state(path, init=init, dry_run=dry_run, republish_note=wording.republish_note)
    if not (init or dry_run) and chat_key not in data:
        raise missing_chat_error(path, chat_key, test, wording)
    store = StateStore(path, data, chat_key)
    if not dry_run and not save_state(data, path):
        raise StateError(f"в {path.name} нельзя писать — отправка отменена")
    return store
