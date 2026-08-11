#!/usr/bin/env python3
"""
Ежедневный урок вьетнамского языка → Telegram.
Запуск: python3 vietnamese_bot.py [--dry-run]
Cron: 0 5 * * * cd /path/to/danang-bots && python3 vietnamese_bot.py >> logs/vietnamese.log 2>&1
"""

import json
import logging
import os
import random
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
log = logging.getLogger("vietnamese_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
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
# Lessons / state I/O
# ---------------------------------------------------------------------------
def load_lessons(path: Path) -> list:
    if not path.exists():
        log.error("Файл уроков не найден: %s", path)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.error("Lessons JSON error: %s", e)
        sys.exit(1)


def _default_state() -> dict:
    return {
        "phase": "course",
        "current_day": 1,
        "last_posted_at": None,
        "last_message_id": None,
        "recent_review_ids": [],
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return _default_state()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.warning("State JSON повреждён (%s) — используем дефолт без перезаписи", e)
        return _default_state()


def save_state(state: dict, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Lesson selection
# ---------------------------------------------------------------------------
def pick_lesson(lessons: list, state: dict) -> tuple[dict, bool]:
    by_day = {l.get("day"): l for l in lessons}
    day = state["current_day"]

    if state["phase"] == "course":
        if day <= 365 and day in by_day:
            return by_day[day], False
        log.info("Урок дня %s недоступен — переключаемся на режим повторения", day)
        state["phase"] = "review"
    elif day <= 365 and day in by_day:
        # Догенерировали новые уроки — возвращаемся к курсу с того же дня
        log.info("Появился урок дня %s — возвращаемся из повторения в курс", day)
        state["phase"] = "course"
        return by_day[day], False

    excluded = set(state.get("recent_review_ids", []))
    filtered = [l for l in lessons if l.get("day") not in excluded]
    if not filtered:
        filtered = lessons
    return random.choice(filtered), True


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_post(lesson: dict, is_review: bool) -> str:
    day = lesson.get("day")
    vietnamese = lesson.get("vietnamese", "")
    transliteration = lesson.get("transliteration_ru", "")
    translation = lesson.get("translation_ru", "")
    breakdown = lesson.get("breakdown", []) or []
    context = lesson.get("context", "")
    tone_tip = lesson.get("tone_tip", "")
    source = (lesson.get("source") or "").lower()
    tags = lesson.get("tags", []) or []

    if is_review:
        header = f"🔁 ПОВТОРЯЕМ — Урок {day} «{vietnamese}»"
    else:
        header = f"🇻🇳 УРОК ВЬЕТНАМСКОГО — День {day} / 365"

    breakdown_lines = []
    for item in breakdown:
        word = item.get("word", "")
        tr = item.get("transliteration", "")
        meaning = item.get("meaning", "")
        breakdown_lines.append(f"• {word} ({tr}) — {meaning}")
    breakdown_block = "\n".join(breakdown_lines)

    parts = [
        header,
        "",
        f"📌 Фраза: {vietnamese}",
        f"🔊 Транскрипция: {transliteration}",
        f"🇷🇺 Перевод: {translation}",
        "",
        "📖 Разбор:",
        breakdown_block,
        "",
        "💬 Когда использовать:",
        context,
        "",
        "🎵 Тон-лайфхак:",
        tone_tip,
    ]

    if "wikibooks" in source:
        parts += ["", "📚 По материалам Wikibooks (CC BY-SA)"]

    if tags:
        parts += ["", " ".join(tags)]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== Vietnamese bot start ===")
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram отправка отключена")

    cfg = load_config()
    base_dir = Path(__file__).parent
    vn_cfg = cfg.get("vietnamese", {}) or {}
    lessons_path = base_dir / vn_cfg.get("lessons_file", "vietnamese_lessons.json")
    state_path = base_dir / vn_cfg.get("state_file", "vietnamese_state.json")

    lessons = load_lessons(lessons_path)
    log.info("Загружено уроков: %d", len(lessons))

    state = load_state(state_path)
    log.info("Состояние: phase=%s, current_day=%s", state.get("phase"), state.get("current_day"))

    lesson, is_review = pick_lesson(lessons, state)
    log.info("Выбран урок: day=%s, is_review=%s", lesson.get("day"), is_review)

    text = format_post(lesson, is_review)
    print("=" * 60)
    print(text)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN — Telegram skipped")
        return

    thread_id = vn_cfg.get("thread_id", 1)
    msg_id = send_telegram_message(cfg, text, thread_id=thread_id)
    if msg_id is None:
        log.error("Telegram отправка провалилась")
        sys.exit(1)

    state["last_posted_at"] = datetime.now(timezone.utc).isoformat()
    state["last_message_id"] = msg_id

    if not is_review:
        state["current_day"] = state.get("current_day", 1) + 1
        if state["current_day"] > 365:
            log.info("Курс завершён после публикации — переключаемся в режим повторения")
            state["phase"] = "review"
    else:
        recent = list(state.get("recent_review_ids", []) or [])
        recent.append(lesson.get("day"))
        state["recent_review_ids"] = recent[-14:]

    save_state(state, state_path)
    log.info("Урок опубликован: day=%s, message_id=%s", lesson.get("day"), msg_id)


if __name__ == "__main__":
    main()
