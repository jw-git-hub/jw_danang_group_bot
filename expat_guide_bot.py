#!/usr/bin/env python3
"""
Еженедельный постер материалов "Гайд экспата" в Telegram-канал Дананга.
Запуск: python3 expat_guide_bot.py [--dry-run]
Cron: 0 4 * * 0 cd /path/to/jw_danang_group_bot && python3 expat_guide_bot.py >> logs/expat_guide.log 2>&1
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from telegram_sender import send_telegram_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("expat_guide_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Config not found: %s", CONFIG_PATH)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Config JSON error: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Маппинг эмодзи блоков
# ---------------------------------------------------------------------------
BLOCK_EMOJI = {
    "visa": "🛂",
    "money": "💰",
    "transport": "🛵",
    "housing": "🏠",
    "health": "🏥",
    "food": "🛒",
    "comm": "📱",
    "misc": "📋",
}

DEFAULT_STATE: dict = {
    "current_index": 1,
    "last_posted_at": None,
    "last_message_id": None,
}


# ---------------------------------------------------------------------------
# Guide / state IO
# ---------------------------------------------------------------------------
def load_guide(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Гайд не найден: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Гайд JSON error: %s", e)
        sys.exit(1)


def load_state(path: Path) -> dict:
    if not path.exists():
        log.info("State-файл не найден, используем дефолт: %s", path)
        return dict(DEFAULT_STATE)
    try:
        with open(path) as f:
            data = json.load(f)
        # Восстанавливаем недостающие поля
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except json.JSONDecodeError as e:
        log.warning("State-файл повреждён (%s), используем дефолт: %s", e, path)
        return dict(DEFAULT_STATE)


def save_state(state: dict, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Поиск материала
# ---------------------------------------------------------------------------
def find_item(guide: list[dict], current_index: int):
    """Возвращает None если индекс превысил max id (курс закончен),
    ("not_ready", item) если body пуст, ("ok", item) если готов к постингу."""
    item = next((x for x in guide if x.get("id") == current_index), None)
    if item is None:
        return None
    body = item.get("body")
    if not body or not body.strip():
        return ("not_ready", item)
    return ("ok", item)


# ---------------------------------------------------------------------------
# Форматирование поста
# ---------------------------------------------------------------------------
def format_post(item: dict) -> str:
    emoji = BLOCK_EMOJI.get(item.get("block", ""), "📋")
    title = item.get("title", "")
    body = item.get("body", "")
    hashtags = " ".join(item.get("hashtags", []))
    return f"{emoji} ГАЙД ЭКСПАТА — {title}\n\n{body}\n\n{hashtags}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== Expat guide bot start ===")
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram отправка отключена")

    cfg = load_config()
    guide_cfg = cfg.get("expat_guide", {}) or {}

    base_dir = Path(__file__).parent
    guide_path = Path(guide_cfg.get("guide_file", base_dir / "expat_guide.json"))
    state_path = Path(guide_cfg.get("state_file", base_dir / "expat_guide_state.json"))
    if not guide_path.is_absolute():
        guide_path = base_dir / guide_path
    if not state_path.is_absolute():
        state_path = base_dir / state_path

    guide = load_guide(guide_path)
    state = load_state(state_path)
    current_index = state["current_index"]
    log.info("Текущий index=%d (всего материалов=%d)", current_index, len(guide))

    result = find_item(guide, current_index)

    if result is None:
        max_id = max((x.get("id", 0) for x in guide), default=0)
        log.info("Гайд экспата завершён, текущий index=%d > max=%d", current_index, max_id)
        sys.exit(0)

    status, item = result
    if status == "not_ready":
        log.warning(
            "Material id=%d ещё не подготовлен (body пуст), пропускаем неделю",
            current_index,
        )
        sys.exit(0)

    # status == "ok"
    text = format_post(item)
    log.info(
        "Material id=%d block=%s title=%r — пост сформирован (%d chars)",
        item.get("id"), item.get("block"), item.get("title"), len(text),
    )

    print("=" * 60)
    print(text)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN: пропускаем отправку, state не трогаем")
        return

    thread_id = guide_cfg.get("thread_id", 1)
    msg_id = send_telegram_message(cfg, text, thread_id=thread_id)
    if msg_id is None:
        log.error("Не удалось отправить материал id=%d в Telegram, state не обновлён", current_index)
        sys.exit(1)

    state["current_index"] = current_index + 1
    state["last_posted_at"] = datetime.now(timezone.utc).isoformat()
    state["last_message_id"] = msg_id
    save_state(state, state_path)

    log.info(
        "Успешно опубликован material id=%d, message_id=%s, следующий index=%d",
        current_index, msg_id, state["current_index"],
    )


if __name__ == "__main__":
    main()
