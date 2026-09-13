#!/usr/bin/env python3
"""Общий sender для отправки сообщений в Telegram-группу проекта Дананг.

Используется ботами для постинга в форумную супергруппу с темами (topics).
Поддерживает retry-логику, обработку 429 (rate limit) и хак General-чата
(thread_id == 1 не передаётся в API, иначе отправка ломается).

Форматирование: по умолчанию текст уходит БЕЗ parse_mode, то есть как есть.
Раньше здесь стоял parse_mode="HTML" при том, что ни один бот HTML-разметку не
формирует — любой символ < или последовательность вида &xx в контенте
превращали отправку в ошибку 400 и срывали публикацию за день. Если пост
действительно содержит разметку, вызывающий передаёт parse_mode явно и сам
экранирует подставляемый текст через escape_html().

Тестовый режим: send_*(..., test=True) отправляет в telegram.test_chat_id
(и telegram.test_thread_id, если задан) вместо рабочей группы. Нужен, чтобы
обкатывать форматирование, не публикуя ничего в живую общину.
"""
import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
BACKOFF = [5, 15, 30]
MAX_ATTEMPTS = 3


def _safe(text: str, token: str) -> str:
    """Вырезает bot-токен из текста перед тем, как он попадёт в лог.

    requests кладёт полный URL запроса (включая /bot<TOKEN>/method) в текст
    исключения при сетевых сбоях, а Telegram может отражать URL в теле ответа
    об ошибке. Без этой замены токен утекает в logs/*.log при первом же 5xx
    или обрыве соединения — а логи не секрет так, как config.json.
    """
    return text.replace(token, "<TOKEN>") if token else text

# Пульс публикаций: каждая успешная отправка отмечается здесь, healthcheck.py
# по этому файлу видит, что рубрика замолчала. Именно отсутствие такого пульса
# позволило гайду экспата 13 недель подряд молча пропускать воскресенья.
HEARTBEAT_PATH = Path(__file__).parent / "heartbeats.json"

# Идентификаторы медиа-блока в rich-статье: id — внутренняя ссылка из разметки
# (tg://photo?id=...), attach-имя — поле multipart-запроса с самим файлом.
PHOTO_BLOCK_ID = "hero"
PHOTO_ATTACH_NAME = "hero_file"


def record_heartbeat(rubric: str, message_id: int, path: Path = HEARTBEAT_PATH) -> None:
    """Отмечает успешную публикацию рубрики. Никогда не роняет отправку.

    Известное ограничение: read-modify-write без блокировки файла. Если две
    рубрики опубликуются в один момент, поздняя запись может перезаписать файл
    поверх ранней и пульс одной из них потеряется за этот цикл. Расписания
    рубрик в cron разнесены по времени, поэтому вероятность коллизии низкая —
    риск принят осознанно, а не проглядели.
    """
    try:
        data = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        data[rubric] = {
            "last_posted_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
        }
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 — пульс не должен ломать публикацию
        log.warning("Не удалось записать пульс для %s: %s", rubric, e)


def escape_html(text: str) -> str:
    """Экранирует текст для parse_mode="HTML" Telegram.

    Telegram требует заменять <, > и &, которые не являются частью тега или
    HTML-сущности. quote=False — кавычки экранировать не нужно, они значимы
    только внутри значений атрибутов, которые мы не подставляем из контента.
    """
    return html.escape(text, quote=False)


def resolve_target(cfg: dict, thread_id: int | None, test: bool) -> tuple[int | str, int | None]:
    """Возвращает (chat_id, thread_id) с учётом тестового режима.

    В тестовом режиме thread_id рабочей группы не имеет смысла — у тестовой
    группы свои темы (или их нет вовсе), поэтому берётся telegram.test_thread_id
    или None.
    """
    tg = cfg["telegram"]
    if not test:
        return tg["chat_id"], thread_id

    test_chat = tg.get("test_chat_id")
    if not test_chat:
        # ValueError, а не KeyError — KeyError оборачивает текст в кавычки при
        # выводе (repr), сообщение читается как мусор вместо понятной подсказки.
        raise ValueError(
            "test=True, но в config.json нет telegram.test_chat_id — "
            "добавьте id тестовой группы (бот должен быть её участником)"
        )
    return test_chat, tg.get("test_thread_id")


def _post_with_retry(bot_token: str, method: str, payload: dict,
                     files: dict | None = None) -> dict | None:
    """POST к Telegram API с retry и обработкой 429. Возвращает result или None.

    files задаётся, когда к запросу прикладывается файл (картинка статьи). В этом
    режиме тело уходит как multipart/form-data, а вложенные объекты вроде
    rich_message приходится сериализовать в JSON-строки вручную — form-data
    не умеет вложенность.
    """
    url = f"{API_ROOT}/bot{bot_token}/{method}"

    for attempt in range(MAX_ATTEMPTS):
        try:
            if files:
                data = {k: (v if isinstance(v, (str, int)) else json.dumps(v, ensure_ascii=False))
                        for k, v in payload.items()}
                # Файл читаем заново на каждой попытке: после неудачной отправки
                # его указатель стоит в конце, и повтор ушёл бы с пустым телом.
                handles = {name: open(path, "rb") for name, (path, _fn, _ct) in files.items()}
                try:
                    resp = requests.post(
                        url, data=data, timeout=120,
                        files={name: (files[name][1], fh, files[name][2])
                               for name, fh in handles.items()},
                    )
                finally:
                    for fh in handles.values():
                        fh.close()
            else:
                resp = requests.post(url, json=payload, timeout=30)
            # 429 — отдельная ветка, не считается за основную попытку
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", BACKOFF[attempt])
                except requests.exceptions.JSONDecodeError:
                    # Фронтенд перед Telegram (например, edge-прокси) иногда отдаёт
                    # 429 с HTML-телом вместо JSON — тогда просто берём наш BACKOFF.
                    retry_after = BACKOFF[attempt]
                log.warning("Telegram 429, retry_after=%ds (attempt %d/%d)",
                            retry_after, attempt + 1, MAX_ATTEMPTS)
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(retry_after)
                    continue
                log.error("Telegram 429 on attempt %d/%d — giving up", MAX_ATTEMPTS, MAX_ATTEMPTS)
                return None
            if resp.status_code >= 500:
                # 5xx от api.telegram.org — транзиентный сбой на их стороне, а не
                # наша ошибка запроса. Без этой ветки resp.json() ниже мог бы даже
                # распарситься (Telegram отдаёт JSON и на 5xx), но result["ok"]
                # там false и код просто сдавался с одной попытки — публикация за
                # день терялась на ровном месте.
                log.warning("Telegram %s attempt %d/%d: HTTP %d — retrying in %ds",
                            method, attempt + 1, MAX_ATTEMPTS, resp.status_code, BACKOFF[attempt])
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF[attempt])
                    continue
                log.error("Telegram %s attempt %d/%d: HTTP %d — giving up",
                          method, MAX_ATTEMPTS, MAX_ATTEMPTS, resp.status_code)
                return None
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("Telegram response not JSON: %s", _safe(resp.text[:500], bot_token))
            return None
        except requests.exceptions.RequestException as e:
            # str(e) у requests содержит полный URL, включая /bot<TOKEN>/... —
            # без _safe() токен утекает прямо в logs/*.log при любом обрыве сети.
            if attempt < MAX_ATTEMPTS - 1:
                log.warning("Telegram %s attempt %d/%d failed: %s — retrying in %ds",
                            method, attempt + 1, MAX_ATTEMPTS, _safe(str(e), bot_token), BACKOFF[attempt])
                time.sleep(BACKOFF[attempt])
                continue
            log.error("Telegram %s attempt %d/%d failed: %s — giving up",
                      method, MAX_ATTEMPTS, MAX_ATTEMPTS, _safe(str(e), bot_token))
            return None

        if not result.get("ok"):
            # ok:false — это постоянная ошибка запроса (например, "message text
            # is empty" или невалидный chat_id): ретрай тут не поможет, в отличие
            # от 429/5xx выше. Сигнатуру не меняем (вызывающие ждут int|None),
            # но явная пометка в логе экономит время при разборе инцидента.
            log.error("Telegram API error (%s), ретрай не поможет: %s",
                      method, _safe(str(result), bot_token))
            return None
        return result["result"]

    return None


def send_telegram_message(
    cfg: dict,
    text: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
    link_preview_url: str | None = None,
    prefer_large_media: bool = False,
    show_above_text: bool = False,
    test: bool = False,
    rubric: str | None = None,
) -> int | None:
    """Отправляет обычное сообщение. Возвращает message_id при успехе, None при провале.

    Если thread_id is None или thread_id == 1, message_thread_id НЕ передаётся в API
    (это хак General-чата в форумной группе — передача 1 ломает отправку).

    parse_mode передаётся в API только если задан явно. Текст при этом должен быть
    уже подготовлен вызывающим (см. escape_html) — sender ничего не экранирует сам,
    иначе он ломал бы намеренную разметку.

    link_preview_url: явный URL для превью. Важно: prefer_large_media и
    prefer_small_media Telegram игнорирует, если URL не задан явно, — на одном
    автодетекте первой ссылки крупное превью не включится.
    """
    bot_token = cfg["telegram"]["bot_token"]
    chat_id, thread_id = resolve_target(cfg, thread_id, test)

    preview: dict = {"is_disabled": False}
    if link_preview_url:
        preview["url"] = link_preview_url
        if prefer_large_media:
            preview["prefer_large_media"] = True
    if show_above_text:
        preview["show_above_text"] = True

    payload = {
        "chat_id": chat_id,
        "text": text,
        # disable_web_page_preview объявлен устаревшим в Bot API 7.0 в пользу
        # link_preview_options; is_disabled=False — это дефолт, превью включено.
        "link_preview_options": preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id is not None and thread_id != 1:
        payload["message_thread_id"] = thread_id

    result = _post_with_retry(bot_token, "sendMessage", payload)
    if result is None:
        return None

    msg_id = result["message_id"]
    log.info("Telegram: sent message_id=%s to chat=%s thread=%s%s",
             msg_id, chat_id, payload.get("message_thread_id", "General"),
             " [TEST]" if test else "")
    # Тестовые отправки пульс не обновляют — иначе сторож считал бы рубрику живой
    if rubric and not test:
        record_heartbeat(rubric, msg_id)
    return msg_id


def send_rich_message(
    cfg: dict,
    rich_html: str,
    thread_id: int | None = None,
    test: bool = False,
    rubric: str | None = None,
    photo_path: str | None = None,
    photo_mime: str = "image/jpeg",
) -> int | None:
    """Отправляет rich-сообщение (Bot API 10.1+) методом sendRichMessage.

    Содержимое передаётся как Rich HTML одной строкой: InputRichMessage требует
    ровно одно из полей html / markdown / blocks. Rich HTML, в отличие от обычного
    parse_mode="HTML", понимает <h1>-<h6>, <p>, <hr/>, <blockquote>, <aside>,
    <details>, <footer>, списки и таблицы; лимит текста 32768 символов вместо 4096.

    Внимание: у sendRichMessage НЕТ link_preview_options, и документация не
    определяет, генерируется ли превью для ссылки внутри rich-сообщения. Для
    постов, которые держатся на превью (новости), это проверяется эмпирически.

    Возвращает message_id — метод отдаёт обычный Message, как и sendMessage.
    """
    if not rich_html or not rich_html.strip():
        # Пустой/пробельный rich_html гарантированно даст 400 от Telegram —
        # отсекаем до сетевого похода, чтобы не тратить попытку retry впустую
        # и не путать этот случай с транзиентным сбоем в логах.
        log.error("send_rich_message: пустой rich_html, отправка не выполняется")
        return None

    bot_token = cfg["telegram"]["bot_token"]
    chat_id, thread_id = resolve_target(cfg, thread_id, test)

    rich: dict = {"html": rich_html}
    files = None
    if photo_path:
        # Картинка становится частью статьи: файл уходит через attach://, а в
        # разметке на него ссылается медиа-блок по внутреннему id. Так новость
        # выглядит статьёй с иллюстрацией — превью ссылки в rich не работает
        # вовсе, поэтому изображение надо отдавать явно.
        rich["html"] = f'<img src="tg://photo?id={PHOTO_BLOCK_ID}"/>' + rich_html
        rich["media"] = [{
            "id": PHOTO_BLOCK_ID,
            "media": {"type": "photo", "media": f"attach://{PHOTO_ATTACH_NAME}"},
        }]
        files = {PHOTO_ATTACH_NAME: (photo_path, "image" + Path(photo_path).suffix, photo_mime)}

    payload = {
        "chat_id": chat_id,
        "rich_message": rich,
    }
    if thread_id is not None and thread_id != 1:
        payload["message_thread_id"] = thread_id

    result = _post_with_retry(bot_token, "sendRichMessage", payload, files=files)
    if result is None:
        return None

    msg_id = result["message_id"]
    log.info("Telegram: sent RICH message_id=%s to chat=%s thread=%s%s%s",
             msg_id, chat_id, payload.get("message_thread_id", "General"),
             " +photo" if photo_path else "",
             " [TEST]" if test else "")
    if rubric and not test:
        record_heartbeat(rubric, msg_id)
    return msg_id
