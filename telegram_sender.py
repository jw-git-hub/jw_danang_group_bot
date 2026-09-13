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

Исходы отправки — три, не два:
  - успех: возвращён message_id;
  - точный отказ: возвращён None (4xx кроме 429, ok:false, исчерпанные ретраи
    429/обрыва при УСТАНОВКЕ соединения, локальные ошибки вроде отсутствующего
    файла картинки) — Telegram точно не принял запрос, вызывающий может
    спокойно слать fallback;
  - неопределённый исход: поднимается SendOutcomeUnknown (ReadTimeout, обрыв
    соединения ПОСЛЕ того как запрос ушёл, HTTP 5xx, HTTP 2xx с телом, которое
    не разобрать) — неизвестно, дошло ли сообщение до чата. Ретраить это
    вслепую нельзя: если Telegram на самом деле принял первый запрос, повтор
    опубликует дубль. Вызывающий код обязан ловить это исключение сам и не
    слать альтернативный (fallback) пост.
"""
from __future__ import annotations

import contextlib
import html
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3.exceptions

log = logging.getLogger(__name__)

# urllib3 логирует "Failed to parse headers (url=https://api.telegram.org/bot<TOKEN>/...)"
# WARNING'ом с полным URL (включая токен) при кривом ответе сервера — в обход
# _safe(). Отправка при этом продолжает работать штатно, поэтому просто не
# даём этому уровню логов урллиба писаться вообще.
logging.getLogger("urllib3").setLevel(logging.ERROR)

API_ROOT = "https://api.telegram.org"
BACKOFF = [5, 15, 30]
MAX_ATTEMPTS = 3
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
READ_TIMEOUT_UPLOAD = 120  # запрос с файлом (фото статьи) — существующее большее значение
MAX_RETRY_AFTER = 60  # дольше 429 не ждём, отдаём как точный отказ


class SendOutcomeUnknown(Exception):
    """Запрос мог дойти до Telegram, но подтверждения доставки нет.

    Поднимается вместо ретрая/None при ReadTimeout, обрыве соединения ПОСЛЕ
    того как запрос ушёл на сервер, HTTP 5xx и HTTP 2xx с телом, которое
    нельзя разобрать как обычный ответ Bot API. Во всех этих случаях
    неизвестно, принял ли Telegram сообщение — слепой ретрай или fallback
    рискуют опубликовать дубль. Вызывающий код должен поймать это исключение
    и НЕ отправлять альтернативный пост.
    """


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

    Битый (не-JSON/не-словарь/не-UTF8) существующий файл не блокирует запись
    пульса навсегда — он просто заменяется свежим с нуля, иначе одна
    повреждённая запись убивала бы мониторинг всех рубрик до ручного
    вмешательства. Пишем через временный файл с уникальным именем (mkstemp
    в той же директории) + os.replace: если бы у двух рубрик, публикующихся
    в один момент, был общий tmp-путь, одна запись могла бы затереть частично
    записанные байты другой ДО переименования и оставить heartbeats.json
    перезаписанным мешаниной из обоих JSON.

    Известное ограничение (осознанно принято, не проглядели): read-modify-write
    без блокировки файла. Если две рубрики опубликуются в один момент, поздняя
    запись всё равно может целиком перезаписать файл поверх ранней и пульс
    одной из них потеряется за этот цикл — расписания рубрик в cron разнесены
    по времени, поэтому вероятность коллизии низкая.
    """
    try:
        data = {}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                log.warning("heartbeats.json повреждён, пересоздаём с нуля: %s", e)
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[rubric] = {
            "last_posted_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
        }
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except Exception as e:  # noqa: BLE001 — пульс не должен ломать публикацию
        log.warning("Не удалось записать пульс для %s: %s", rubric, e)


def escape_html(text: str) -> str:
    """Экранирует текст для parse_mode="HTML" Telegram.

    Telegram требует заменять <, > и &, которые не являются частью тега или
    HTML-сущности. quote=False — кавычки экранировать не нужно, они значимы
    только внутри значений атрибутов, которые мы не подставляем из контента.
    """
    return html.escape(text, quote=False)


def _normalize_thread_id(thread_id: int | str | None) -> int | None:
    """int или строка-число -> int; всё остальное (включая None) -> None.

    thread_id приходит то из кода (int-литералы вроде 1451), то из
    config.json (там его иногда записывают строкой) — нормализуем в одном
    месте, чтобы оба send_* не дублировали проверку и не расходились.
    """
    if isinstance(thread_id, bool):
        return None
    if isinstance(thread_id, int):
        return thread_id
    if isinstance(thread_id, str) and thread_id.strip().isdigit():
        return int(thread_id.strip())
    return None


def resolve_target(cfg: dict, thread_id: int | None, test: bool) -> tuple[int | str, int | None]:
    """Возвращает (chat_id, thread_id) с учётом тестового режима.

    В тестовом режиме thread_id рабочей группы не имеет смысла — у тестовой
    группы свои темы (или их нет вовсе), поэтому берётся telegram.test_thread_id
    или None. thread_id всегда возвращается нормализованным — см.
    _normalize_thread_id.
    """
    tg = cfg["telegram"]
    if not test:
        return tg["chat_id"], _normalize_thread_id(thread_id)

    test_chat = tg.get("test_chat_id")
    if not test_chat:
        # ValueError, а не KeyError — KeyError оборачивает текст в кавычки при
        # выводе (repr), сообщение читается как мусор вместо понятной подсказки.
        raise ValueError(
            "test=True, но в config.json нет telegram.test_chat_id — "
            "добавьте id тестовой группы (бот должен быть её участником)"
        )
    return test_chat, _normalize_thread_id(tg.get("test_thread_id"))


def _root_cause_chain(exc: BaseException):
    """Обходит exc через args[0] (и MaxRetryError.reason), __cause__, __context__.

    Так внутри requests.ConnectionError находится исходное urllib3-исключение:
    requests заворачивает его как ConnectionError(MaxRetryError(..., reason=X))
    либо через implicit chaining (__context__), когда возбуждает новое
    исключение внутри своего except-блока без explicit `from`.
    """
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        if isinstance(e, urllib3.exceptions.MaxRetryError):
            stack.append(e.reason)
        args0 = e.args[0] if getattr(e, "args", None) else None
        if isinstance(args0, BaseException):
            stack.append(args0)
        stack.append(e.__cause__)
        stack.append(e.__context__)


def _is_establish_failure(exc: BaseException) -> bool:
    """True, если ConnectionError вызван тем, что соединение не удалось
    УСТАНОВИТЬ (DNS не резолвится, в соединении отказано, connect-таймаут
    внутри urllib3 — NewConnectionError/ConnectTimeoutError) — тогда точно
    ничего не ушло на сервер и ретрай безопасен. Любая другая ConnectionError
    (обрыв уже установленного соединения на середине обмена и т.п.) —
    неопределённый исход, ретраить вслепую нельзя.
    """
    return any(
        isinstance(e, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError))
        for e in _root_cause_chain(exc)
    )


def _post_with_retry(bot_token: str, method: str, payload: dict,
                     files: dict | None = None) -> dict | None:
    """POST к Telegram API. Возвращает result при успехе, None при точном отказе.

    При неопределённом исходе (см. докстринг модуля) поднимает
    SendOutcomeUnknown вместо возврата None — вызывающий обязан это ловить и
    не слать альтернативный пост вместо этого.

    files задаётся, когда к запросу прикладывается файл (картинка статьи). В этом
    режиме тело уходит как multipart/form-data, а вложенные объекты вроде
    rich_message приходится сериализовать в JSON-строки вручную — form-data
    не умеет вложенность.
    """
    url = f"{API_ROOT}/bot{bot_token}/{method}"
    timeout = (CONNECT_TIMEOUT, READ_TIMEOUT_UPLOAD if files else READ_TIMEOUT)

    for attempt in range(MAX_ATTEMPTS):
        last = attempt == MAX_ATTEMPTS - 1
        try:
            if files:
                try:
                    handles = {name: open(path, "rb") for name, (path, _fn, _ct) in files.items()}
                except OSError as e:
                    # Локальная ошибка (файла нет/недоступен для чтения) — на
                    # повторной попытке то же самое, ретраить нечего.
                    log.error("Telegram %s: файл для отправки недоступен: %s", method, e)
                    return None
                data = {k: (v if isinstance(v, (str, int)) else json.dumps(v, ensure_ascii=False))
                        for k, v in payload.items()}
                # Файл читаем заново на каждой попытке: после неудачной отправки
                # его указатель стоит в конце, и повтор ушёл бы с пустым телом.
                try:
                    resp = requests.post(
                        url, data=data, timeout=timeout,
                        files={name: (files[name][1], fh, files[name][2])
                               for name, fh in handles.items()},
                    )
                finally:
                    for fh in handles.values():
                        fh.close()
            else:
                resp = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectTimeout:
            # Соединение не установилось за отведённое время — точно ничего
            # не отправилось, безопасно ретраить.
            if last:
                log.error("Telegram %s attempt %d/%d: connect timeout — giving up",
                          method, MAX_ATTEMPTS, MAX_ATTEMPTS)
                return None
            log.warning("Telegram %s attempt %d/%d: connect timeout — retrying in %ds",
                        method, attempt + 1, MAX_ATTEMPTS, BACKOFF[attempt])
            time.sleep(BACKOFF[attempt])
            continue
        except requests.exceptions.ReadTimeout as e:
            # Запрос ушёл, ответ не пришёл вовремя — неизвестно, принял ли
            # Telegram сообщение. Слепой ретрай рискует опубликовать дубль.
            log.warning("Telegram %s: read timeout, доставка не подтверждена — не ретраим: %s",
                        method, _safe(str(e), bot_token))
            raise SendOutcomeUnknown(_safe(f"Telegram {method}: read timeout: {e}", bot_token)) from None
        except (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL, urllib3.exceptions.LocationParseError) as e:
            # Локальная ошибка URL — при повторе результат тот же, ретрай бессмысленен.
            log.error("Telegram %s: некорректный URL запроса: %s", method, _safe(str(e), bot_token))
            return None
        except requests.exceptions.ConnectionError as e:
            if _is_establish_failure(e):
                if last:
                    log.error("Telegram %s attempt %d/%d: %s — giving up",
                              method, MAX_ATTEMPTS, MAX_ATTEMPTS, _safe(str(e), bot_token))
                    return None
                log.warning("Telegram %s attempt %d/%d failed: %s — retrying in %ds",
                            method, attempt + 1, MAX_ATTEMPTS, _safe(str(e), bot_token), BACKOFF[attempt])
                time.sleep(BACKOFF[attempt])
                continue
            # Соединение было установлено и оборвалось уже после этого —
            # неизвестно, дошёл ли запрос до Telegram.
            log.warning("Telegram %s: соединение оборвалось, доставка не подтверждена — не ретраим: %s",
                        method, _safe(str(e), bot_token))
            raise SendOutcomeUnknown(_safe(f"Telegram {method}: {e}", bot_token)) from None
        except (requests.exceptions.ChunkedEncodingError, urllib3.exceptions.ProtocolError) as e:
            log.warning("Telegram %s: обрыв при чтении ответа, доставка не подтверждена — не ретраим: %s",
                        method, _safe(str(e), bot_token))
            raise SendOutcomeUnknown(_safe(f"Telegram {method}: {e}", bot_token)) from None
        except requests.exceptions.RequestException as e:
            # Всё прочее сетевое (SSL и т.п.) — не знаем, дошёл ли запрос,
            # безопаснее считать неопределённым исходом, а не точным отказом.
            log.warning("Telegram %s: %s, доставка не подтверждена — не ретраим: %s",
                        method, type(e).__name__, _safe(str(e), bot_token))
            raise SendOutcomeUnknown(_safe(f"Telegram {method}: {type(e).__name__}: {e}", bot_token)) from None

        # 429 — отдельная ветка, не считается за основную попытку
        if resp.status_code == 429:
            try:
                body = resp.json()
            except requests.exceptions.JSONDecodeError:
                # Фронтенд перед Telegram (например, edge-прокси) иногда отдаёт
                # 429 с HTML-телом вместо JSON — тогда просто берём наш BACKOFF.
                retry_after = BACKOFF[attempt]
            else:
                if isinstance(body, dict):
                    params = body.get("parameters")
                    retry_after = (params.get("retry_after", BACKOFF[attempt])
                                   if isinstance(params, dict) else BACKOFF[attempt])
                else:
                    # JSON распарсился, но это не объект (например, список) —
                    # .get() уронил бы AttributeError; берём дефолт.
                    retry_after = 5
            if retry_after > MAX_RETRY_AFTER:
                log.error("Telegram %s: 429 retry_after=%ds больше лимита %ds — giving up без ожидания",
                          method, retry_after, MAX_RETRY_AFTER)
                return None
            if last:
                log.error("Telegram %s: 429 on attempt %d/%d — giving up", method, MAX_ATTEMPTS, MAX_ATTEMPTS)
                return None
            log.warning("Telegram %s 429, retry_after=%ds (attempt %d/%d)",
                        method, retry_after, attempt + 1, MAX_ATTEMPTS)
            time.sleep(retry_after)
            continue

        if resp.status_code >= 500:
            # 5xx от api.telegram.org: сервер мог как отклонить запрос, так и
            # принять его перед тем как упасть, — неопределённый исход, не ретраим.
            log.warning("Telegram %s: HTTP %d, доставка не подтверждена — не ретраим: %s",
                        method, resp.status_code, _safe(resp.text[:300], bot_token))
            raise SendOutcomeUnknown(_safe(
                f"Telegram {method}: HTTP {resp.status_code}: {resp.text[:300]}", bot_token))

        if resp.status_code >= 400:
            # 4xx (кроме 429) — постоянная ошибка запроса (например, "message
            # text is empty" или невалидный chat_id): ретрай тут не поможет.
            log.error("Telegram API HTTP %d (%s), ретрай не поможет: %s",
                      resp.status_code, method, _safe(resp.text[:500], bot_token))
            return None

        try:
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.warning("Telegram %s: HTTP %d, тело не JSON — доставка не подтверждена: %s",
                        method, resp.status_code, _safe(resp.text[:300], bot_token))
            raise SendOutcomeUnknown(_safe(
                f"Telegram {method}: HTTP {resp.status_code} тело не JSON: {resp.text[:300]}", bot_token))

        if not isinstance(result, dict) or "ok" not in result:
            log.warning("Telegram %s: HTTP %d, JSON без 'ok' — доставка не подтверждена: %s",
                        method, resp.status_code, _safe(str(result)[:300], bot_token))
            raise SendOutcomeUnknown(_safe(
                f"Telegram {method}: HTTP {resp.status_code} JSON без 'ok': {str(result)[:300]}", bot_token))

        if not result.get("ok"):
            # ok:false — это постоянная ошибка запроса (например, "message text
            # is empty" или невалидный chat_id): ретрай тут не поможет, в отличие
            # от 429/5xx выше. Сигнатуру не меняем (вызывающие ждут int|None),
            # но явная пометка в логе экономит время при разборе инцидента.
            log.error("Telegram API error (%s), ретрай не поможет: %s",
                      method, _safe(str(result), bot_token))
            return None

        if not isinstance(result.get("result"), dict):
            # ok:true, но 'result' отсутствует или не объект (например, {"ok":true}
            # без result вовсе, или result — список/bool) — не тот ответ, которого
            # ждут вызывающие (sendMessage/sendRichMessage всегда возвращают Message
            # как объект). Запрос мог реально дойти и что-то опубликовать — как и
            # при 5xx/нечитаемом теле выше, это неопределённый исход, а не точный
            # отказ: KeyError/TypeError ниже по стеку ушли бы необработанными.
            log.warning("Telegram %s: HTTP %d, ok=true но 'result' отсутствует или не объект — "
                        "доставка не подтверждена: %s",
                        method, resp.status_code, _safe(str(result)[:300], bot_token))
            raise SendOutcomeUnknown(_safe(
                f"Telegram {method}: HTTP {resp.status_code} ok=true, 'result' некорректен: "
                f"{str(result)[:300]}", bot_token))

        return result["result"]

    return None


def _require_message_id(method: str, result: dict, bot_token: str) -> int:
    """Достаёт message_id из result и проверяет, что это целое число.

    ok:true с объектным result, в котором нет числового message_id (не тот
    метод, неожиданный формат ответа), — тоже неопределённый исход: сообщение
    могло реально уйти, а подтвердить это нечем. Поднимает SendOutcomeUnknown
    вместо KeyError/TypeError, которые иначе ушли бы наружу необработанными.
    """
    msg_id = result.get("message_id")
    if not isinstance(msg_id, int) or isinstance(msg_id, bool):
        log.warning("Telegram %s: ok=true, но в result нет целочисленного message_id — "
                    "доставка не подтверждена: %s", method, _safe(str(result)[:300], bot_token))
        raise SendOutcomeUnknown(_safe(
            f"Telegram {method}: ok=true, 'result' без message_id: {str(result)[:300]}", bot_token))
    return msg_id


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
    """Отправляет обычное сообщение. Возвращает message_id при успехе, None при
    точном отказе. При неопределённом исходе поднимает SendOutcomeUnknown —
    вызывающий обязан её поймать и не слать fallback (риск дубля).

    thread_id нормализуется (int либо строка-число); если результат — None, 0
    или 1, message_thread_id НЕ передаётся в API (это хак General-чата в
    форумной группе — передача 1 ломает отправку).

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
    if thread_id not in (None, 0, 1):
        payload["message_thread_id"] = thread_id

    result = _post_with_retry(bot_token, "sendMessage", payload)
    if result is None:
        return None

    msg_id = _require_message_id("sendMessage", result, bot_token)
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

    Возвращает message_id при успехе, None при точном отказе. При неопределённом
    исходе поднимает SendOutcomeUnknown — вызывающий обязан её поймать и не
    слать fallback (риск дубля).
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
    if thread_id not in (None, 0, 1):
        payload["message_thread_id"] = thread_id

    result = _post_with_retry(bot_token, "sendRichMessage", payload, files=files)
    if result is None:
        return None

    msg_id = _require_message_id("sendRichMessage", result, bot_token)
    log.info("Telegram: sent RICH message_id=%s to chat=%s thread=%s%s%s",
             msg_id, chat_id, payload.get("message_thread_id", "General"),
             " +photo" if photo_path else "",
             " [TEST]" if test else "")
    if rubric and not test:
        record_heartbeat(rubric, msg_id)
    return msg_id
