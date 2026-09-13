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
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from dedup import extract_fingerprint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# Лимит хранения записей трекера. Раньше был равен окну чтения ридера (200
# сообщений) — это значит, что каждый новый пост вытеснял запись, которую
# ридер потом заново импортировал при следующем прогоне, и память дедупа
# непрерывно "тасовалась". Подняли до 1000, чтобы история была стабильнее.
TRACKER_LIMIT = 1000

# Сколько дней хранить окна публикации (windows) — ключ никогда не
# обрезался и рос бесконечно (уже 212 записей на живых данных).
WINDOWS_RETENTION_DAYS = 14


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
    tracker_path = BASE_DIR / cfg["news"]["tracker_file"]
    if tracker_path.exists():
        try:
            with open(tracker_path) as f:
                return json.load(f), tracker_path
        except json.JSONDecodeError as e:
            # БЛОКЕР (было): при битом JSON функция возвращала ПУСТОЙ трекер,
            # а следующее сохранение затирало им файл — пять месяцев истории
            # дедупа исчезали молча, и бот начинал перепощивать старое.
            # Теперь: сохраняем повреждённую копию рядом для разбора и
            # падаем с ненулевым кодом — трогать существующий файл нельзя.
            corrupt_path = tracker_path.with_name(tracker_path.name + ".corrupt")
            try:
                shutil.copy2(tracker_path, corrupt_path)
                log.error(
                    "Tracker file corrupted (%s). Saved a copy to %s. Refusing "
                    "to continue with an empty tracker.", e, corrupt_path,
                )
            except OSError as copy_err:
                log.error(
                    "Tracker file corrupted (%s), and failed to save a copy (%s). "
                    "Refusing to continue with an empty tracker.", e, copy_err,
                )
            sys.exit(1)
    return {"urls": [], "headlines": [], "posts": [], "windows": {}}, tracker_path


def _posted_at_key(post):
    """Ключ сортировки по времени публикации записи.
    Если даты нет или её не удалось разобрать — считаем запись самой свежей
    (безопасное поведение по умолчанию: лучше не потерять запись при
    обрезке, чем ошибочно выбросить её как "старую")."""
    posted_at = post.get("posted_at") if isinstance(post, dict) else None
    if posted_at:
        try:
            dt = datetime.fromisoformat(posted_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
    return datetime.max.replace(tzinfo=timezone.utc)


def _trim_windows(tracker, days=WINDOWS_RETENTION_DAYS):
    """windows раньше не обрезался никогда и рос вечно. Оставляем только
    окна за последние N дней (значение — ISO-timestamp последнего поста
    в это окно)."""
    windows = tracker.get("windows")
    if not isinstance(windows, dict) or not windows:
        return
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    kept = {}
    for key, value in windows.items():
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Не смогли разобрать дату — оставляем окно, чтобы не потерять
            # данные из-за неожиданного формата.
            kept[key] = value
            continue
        if dt >= cutoff:
            kept[key] = value
    tracker["windows"] = kept


def save_tracker(tracker, tracker_path):
    posts = tracker.get("posts", [])
    if posts:
        # ВАЖНО (было): обрезка list[-LIMIT:] предполагает хронологический
        # порядок, которого нет — ридер добавляет сообщения от новых к
        # старым (на живых данных 73 из 199 соседних пар в posts идут не по
        # времени). Наивная обрезка выбрасывала не самые старые записи.
        # Сортируем posts по posted_at и синхронно переставляем
        # urls/headlines/fingerprints — они пополняются в том же порядке,
        # что и posts, поэтому синхронизируем их по позиции.
        n = len(posts)
        paired_keys = ["urls", "headlines", "fingerprints"]
        syncable = [k for k in paired_keys if len(tracker.get(k, [])) == n]
        order = sorted(range(n), key=lambda i: _posted_at_key(posts[i]))
        tracker["posts"] = [posts[i] for i in order]
        for key in syncable:
            values = tracker[key]
            tracker[key] = [values[i] for i in order]
        # Если длина списка разошлась с posts (не должно происходить в
        # норме) — синхронизировать нечем, обрезаем по-старому как запасной
        # безопасный путь, чтобы не уронить сохранение.
        for key in paired_keys:
            if key not in syncable and key in tracker:
                tracker[key] = tracker[key][-TRACKER_LIMIT:]

    for key in ["urls", "headlines", "posts", "fingerprints"]:
        if key in tracker:
            tracker[key] = tracker[key][-TRACKER_LIMIT:]

    _trim_windows(tracker)

    # БЛОКЕР (было): файл открывался на запись (усекался), и только потом
    # писался JSON — сбой посреди записи оставлял битый файл. Пишем во
    # временный файл рядом и атомарно заменяем через os.replace.
    tmp_path = tracker_path.with_name(tracker_path.name + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, tracker_path)
    log.info("Tracker saved: %s", tracker_path)


def extract_url_from_text(text):
    """Extract source URL from post text (looks for 📰 Источник: URL pattern)."""
    if not text:
        return None
    # Pattern: 📰 Источник: URL
    m = re.search(r"Источник:\s*(https?://\S+)", text)
    if m:
        return m.group(1).rstrip(")")
    # Fallback: any URL in text
    m = re.search(r"https?://\S+", text)
    if m:
        url = m.group(0).rstrip(")")
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
                if msg.text or msg.message:
                    text = msg.text or msg.message
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


async def main():
    cfg = load_config()
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
