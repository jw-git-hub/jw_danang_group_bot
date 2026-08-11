#!/usr/bin/env python3
"""
Read news thread history from Telegram using user account (Telethon).
Populates the dedup tracker with already-posted news.
Uses the user account ONLY for reading — never for posting.

First run requires interactive auth (phone + code).
Subsequent runs use saved session.

Usage: python3 read_history.py
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
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
        except json.JSONDecodeError:
            log.warning("Tracker file corrupted, starting fresh")
    return {"urls": [], "headlines": [], "posts": [], "windows": {}}, tracker_path


def save_tracker(tracker, tracker_path):
    for key in ["urls", "headlines", "posts", "fingerprints"]:
        if key in tracker:
            tracker[key] = tracker[key][-200:]
    with open(tracker_path, "w") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
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

    try:
        await client.start()
        log.info("Telethon client connected")

        chat_id = int(cfg["telegram"]["chat_id"])
        thread_id = cfg["telegram"]["news_thread_id"]

        entity = await client.get_entity(chat_id)
        log.info("Chat: %s", entity.title)

        messages = []
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
            log.warning("Telegram flood wait: sleeping %d seconds", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            log.info("Returning %d messages collected before flood wait", len(messages))
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
