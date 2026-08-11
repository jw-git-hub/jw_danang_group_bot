#!/usr/bin/env python3
"""Общий sender для отправки сообщений в Telegram-группу проекта Дананг.

Используется ботами для постинга в форумную супергруппу с темами (topics).
Поддерживает retry-логику, обработку 429 (rate limit) и хак General-чата
(thread_id == 1 не передаётся в API, иначе отправка ломается).
"""
import logging
import time

import requests

log = logging.getLogger(__name__)


def send_telegram_message(cfg: dict, text: str, thread_id: int | None = None) -> int | None:
    """Отправляет сообщение в Telegram. Возвращает message_id при успехе, None при провале.

    Если thread_id is None или thread_id == 1, message_thread_id НЕ передаётся в API
    (это хак General-чата в форумной группе — передача 1 ломает отправку).
    """
    bot_token = cfg["telegram"]["bot_token"]
    chat_id = cfg["telegram"]["chat_id"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if thread_id is not None and thread_id != 1:
        payload["message_thread_id"] = thread_id

    backoff = [5, 15, 30]
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            # Handle Telegram 429 (rate limit) — отдельная ветка, не считается за основную попытку
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", backoff[attempt])
                log.warning("Telegram 429, retry_after=%ds (attempt %d/3)", retry_after, attempt + 1)
                if attempt < 2:
                    time.sleep(retry_after)
                    continue
                else:
                    log.error("Telegram 429 on attempt 3/3 — giving up")
                    return None
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("Telegram response not JSON: %s", resp.text[:500])
            return None
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                log.warning("Telegram send attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, backoff[attempt])
                time.sleep(backoff[attempt])
                continue
            else:
                log.error("Telegram send attempt 3/3 failed: %s — giving up", e)
                return None

        if not result.get("ok"):
            log.error("Telegram API error: %s", result)
            return None
        msg_id = result["result"]["message_id"]
        log.info("Telegram: sent message_id=%s to thread=%s", msg_id, payload.get("message_thread_id", "General"))
        return msg_id
    return None
