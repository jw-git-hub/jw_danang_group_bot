#!/usr/bin/env python3
"""
Read news thread history from Telegram using user account (Telethon).
Populates the dedup tracker with already-posted news.
Uses the user account ONLY for reading — never for posting.

First run requires interactive auth (phone + code) — run manually once
to create reader_session.session. This script itself must NEVER prompt
for input: it runs from cron, and an unattended input() call would hang
the process forever while holding the session file.

Usage: python3 read_history.py
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

# load_tracker/save_tracker — единая реализация в dedup.py (см. её докстринг):
# было по одной несовместимой копии на каждый скрипт (лимит хранения раньше
# был здесь равен окну чтения ридера — 200 сообщений, — что означало, что
# каждый новый пост вытеснял запись, которую ридер тут же переимпортировал).
# Тонкие обёртки ниже сохраняют прежние сигнатуры (tracker, tracker_path).
from dedup import extract_fingerprint
from dedup import load_tracker as _dedup_load_tracker
from dedup import save_tracker as _dedup_save_tracker
from dedup import acquire_lock, tracker_lock_path as _dedup_lock_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def load_config():
    try:
        with open(BASE_DIR / "config.json") as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Config not found: %s", BASE_DIR / "config.json")
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Config JSON error: %s", e)
        sys.exit(1)


def load_tracker(cfg):
    """read_history.py — инструмент ВОССТАНОВЛЕНИЯ трекера: в отличие от
    news_bot.py (см. C2), отсутствие файла здесь не ошибка, а нормальный
    сценарий первого запуска на новом сервере — просто начинаем с пустого
    трекера и наполняем его из истории треда."""
    tracker_path = BASE_DIR / cfg["news"]["tracker_file"]
    return _dedup_load_tracker(tracker_path), tracker_path


def save_tracker(tracker, tracker_path):
    _dedup_save_tracker(tracker_path, tracker)


def extract_url_from_text(text):
    """Extract source URL from post text (looks for 📰 Источник: URL pattern).

    Ожидает text = msg.message (см. read_news_thread) — сырой текст без
    markdown-обёртки, но регулярка всё равно терпима к markdown-ссылке вида
    "[URL](URL)" (необязательный "[" перед URL, ")"/"]" не входят в захват)
    и хвостовой пунктуации из естественного текста ("...html." в конце
    предложения).
    """
    if not text:
        return None
    # Pattern: 📰 Источник: URL, с необязательной markdown-ссылкой "[URL](URL)"
    m = re.search(r"Источник:\s*\[?(https?://[^\s\])]+)", text)
    if m:
        return m.group(1).rstrip(".,;")
    # Fallback: any URL in text
    m = re.search(r"https?://[^\s\])]+", text)
    if m:
        url = m.group(0).rstrip(".,;")
        # Skip telegram URLs
        if "t.me" not in url and "telegram" not in url:
            return url
    return None


def extract_headline_from_text(text):
    """Extract headline from post (first line, usually in CAPS with emojis)."""
    if not text:
        return None
    lines = text.strip().split("\n")
    if lines:
        # Remove emojis and clean up
        headline = lines[0].strip()
        return headline if len(headline) > 10 else None
    return None


async def read_news_thread(cfg):
    """Read all messages from the news thread."""
    reader = cfg["reader"]
    client = TelegramClient(
        str(BASE_DIR / "reader_session"),
        reader["api_id"],
        reader["api_hash"],
    )

    messages = []
    thread_id = cfg["telegram"]["news_thread_id"]

    try:
        try:
            await client.connect()
        except Exception as e:
            # Подключение обёрнуто в try/except: раньше здесь уже случались
            # трейсбеки ConnectionError, которые роняли процесс без внятного
            # выхода.
            log.error("Failed to connect Telethon client: %s", e)
            sys.exit(1)

        if not await client.is_user_authorized():
            # БЛОКЕР (было): client.start() без аргументов при протухшей
            # сессии уходит в input() за номером телефона. Скрипт работает
            # по cron — процесс повиснет навсегда, удерживая файл сессии.
            # Никогда не запрашиваем ввод: логируем и выходим с ошибкой.
            log.error(
                "Telethon session is not authorized. Run interactive login "
                "manually first (this script must never prompt for a "
                "phone/code — it runs unattended from cron)."
            )
            sys.exit(1)

        log.info("Telethon client connected")

        chat_id = int(cfg["telegram"]["chat_id"])

        entity = await client.get_entity(chat_id)
        # МЕЛОЧЬ (было): entity.title даёт AttributeError, если сущность
        # окажется не чатом (например, User). Безопасное получение атрибута.
        chat_name = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or str(chat_id)
        )
        log.info("Chat: %s", chat_name)

        try:
            async for msg in client.iter_messages(
                entity,
                reply_to=thread_id,
                limit=200,
            ):
                if msg.message or msg.text:
                    # msg.message — сырой текст без markdown-обёртки; msg.text
                    # (форматированный markdown) оборачивает ссылки в
                    # "[URL](URL)" и ломал бы regex-извлечение URL ниже.
                    text = msg.message or msg.text
                    messages.append({
                        "id": msg.id,
                        "date": msg.date.isoformat(),
                        "text": text,
                    })
        except FloodWaitError as e:
            # ВАЖНО (было): sleep(e.seconds + 1), после чего функция всё
            # равно выходила без повтора — сон был бессмысленным, а
            # Telegram может попросить подождать порядка суток, из-за чего
            # процесс залипал. Не спим, логируем величину задержки и
            # выходим.
            log.error(
                "Telegram flood wait: %d seconds required — aborting "
                "without sleeping (this is a cron job; sleeping could hang "
                "it for hours)", e.seconds,
            )
            sys.exit(1)
        except Exception as e:
            log.error("Error reading messages: %s", e)

    finally:
        await client.disconnect()

    log.info("Read %d messages from thread %s", len(messages), thread_id)
    return messages


def populate_tracker(messages, tracker):
    """Add messages from thread to the dedup tracker."""
    added = 0
    # Build set of existing telegram_message_ids for O(1) lookup
    existing_ids = {p["telegram_message_id"] for p in tracker.get("posts", [])
                    if "telegram_message_id" in p}

    for msg in messages:
        text = msg["text"]

        # Skip messages that don't look like bot-posted news
        # Valid news posts always contain "📰 Источник:" pattern
        if "Источник:" not in text:
            log.debug("Skipping non-news message %d (no Источник:)", msg["id"])
            continue

        # Skip if this telegram_message_id is already in tracker
        if msg["id"] in existing_ids:
            log.debug("Skipping already-tracked message %d", msg["id"])
            continue

        url = extract_url_from_text(text)
        headline = extract_headline_from_text(text)

        if not url and not headline:
            continue

        # Skip if already tracked by URL
        if url and url in tracker.get("urls", []):
            continue

        if url:
            tracker.setdefault("urls", []).append(url)
        if headline:
            tracker.setdefault("headlines", []).append(headline)

        # Compute fingerprint from headline + URL (cross-language)
        fp = extract_fingerprint(headline or "", url)
        tracker.setdefault("fingerprints", []).append(list(fp))

        tracker.setdefault("posts", []).append({
            "url": url or "",
            "headline": headline or "",
            "en_title": "",
            "posted_at": msg["date"],
            "telegram_message_id": msg["id"],
            "window": "imported",
            "platform": "telegram",
        })
        added += 1
        log.info("Added: %s | %s", (headline or "")[:60], (url or "no URL")[:80])

    return added


def check_argv():
    """read_history.py не принимает аргументов — неожиданный флаг (опечатка
    в ручном запуске) лучше провалить явно и сразу, чем молча проигнорировать."""
    if len(sys.argv) > 1:
        print(f"Usage: {sys.argv[0]}\nUnknown argument(s): {' '.join(sys.argv[1:])}",
              file=sys.stderr)
        sys.exit(2)


async def main():
    check_argv()
    cfg = load_config()

    # news_bot.py и read_history.py читают/пишут один и тот же tracker_file —
    # без общего лока конкурентный запуск (например, ручной read_history.py
    # поверх ещё не завершившегося cron-прогона news_bot.py) мог бы потерять
    # запись при сохранении или задвоить публикацию.
    tracker_path = BASE_DIR / cfg["news"]["tracker_file"]
    _lock_fh = acquire_lock(_dedup_lock_path(tracker_path))  # noqa: F841 — держим ссылку, чтобы лок не снялся раньше времени

    tracker, tracker_path = load_tracker(cfg)

    log.info("=== Reading news thread history ===")
    messages = await read_news_thread(cfg)

    if not messages:
        log.warning("No messages found in thread")
        return

    log.info("=== Populating tracker ===")
    added = populate_tracker(messages, tracker)

    save_tracker(tracker, tracker_path)
    log.info("=== Done: %d new entries added to tracker ===", added)


if __name__ == "__main__":
    asyncio.run(main())
