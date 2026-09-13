#!/usr/bin/env python3
"""Скачивание заглавной картинки статьи для новостного поста.

Картинка нужна, чтобы новость выглядела статьёй, а не ссылкой с превью:
rich-сообщение Telegram умеет медиа-блок внутри текста, но превью ссылки
в нём не генерируется вовсе — значит изображение надо отдать явно.

Файл живёт на диске ровно столько, сколько идёт публикация: скачали → отправили
→ удалили. Для этого модуль даёт контекстный менеджер, который убирает файл
даже если отправка упала с исключением.

Источник URL (og:image со стороннего сайта) не доверенный: скачивание целиком
обёрнуто в try/except Exception — отдельные хосты отдают мусор вида
"https://cdn..example.com/x.jpg" (двойная точка, urllib3 LocationParseError),
битые IPv6-литералы, порты не-числом и т.п., а диск может быть переполнен
(OSError из tempfile.mkstemp). Ни один из этих сбоев не должен ронять
публикацию новости — в худшем случае она уйдёт без иллюстрации.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; DanangBot/1.0; +https://t.me/rus_danang)"
DOWNLOAD_TIMEOUT = 30
# Telegram отвергает фото больше 10 МБ; берём запас и не тратим диск на мусор
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_IMAGE_BYTES = 5 * 1024  # меньше — это иконка-заглушка, а не иллюстрация

EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Сигнатуры (magic bytes) в начале файла — контент-тайп из ответа сервера не
# заслуживает доверия сам по себе: сайт может отдать HTML-страницу ошибки с
# Content-Type: image/jpeg, и без этой проверки такой "битый" файл ушёл бы в
# Telegram как есть.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _looks_like_image(head: bytes) -> bool:
    if head.startswith(_JPEG_MAGIC) or head.startswith(_PNG_MAGIC):
        return True
    # WEBP: RIFF <4 байта размера> WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return False


@contextlib.contextmanager
def downloaded_image(url: Optional[str]) -> Iterator[Optional[str]]:
    """Качает картинку во временный файл и гарантированно удаляет его на выходе.

    Отдаёт путь к файлу либо None, если картинки нет или она не подошла —
    вызывающий сам решает, публиковать без иллюстрации или откатываться
    на обычное сообщение с превью ссылки.
    """
    path = None
    try:
        path = _download(url) if url else None
        yield path
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log.info("Временный файл картинки удалён: %s", path)
            except OSError as e:
                log.warning("Не удалось удалить временный файл %s: %s", path, e)


def _download(url: str) -> Optional[str]:
    # Общий бюджет на скачивание поверх per-read таймаутов requests: сервер,
    # отдающий данные по чуть-чуть (по байту раз в 29 секунд), укладывается в
    # каждый отдельный read-таймаут, но без этой проверки может растягивать
    # публикацию новости на неопределённое время.
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    resp = None
    path = None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=DOWNLOAD_TIMEOUT, stream=True)

        if resp.status_code != 200:
            log.warning("Картинка отдала HTTP %s: %s", resp.status_code, url[:100])
            return None

        mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if mime not in EXT_BY_MIME:
            log.warning("Не картинка или неподдерживаемый тип (%s): %s", mime or "нет типа", url[:100])
            return None

        # Верим Content-Length только как быстрой отсечке: сервер может соврать,
        # поэтому реальный размер всё равно контролируем по ходу скачивания.
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            log.warning("Картинка слишком большая (%s байт заявлено): %s", declared, url[:100])
            return None

        fd, path = tempfile.mkstemp(suffix=EXT_BY_MIME[mime], prefix="danang_news_")
        size = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"скачивание картинки превысило общий лимит {DOWNLOAD_TIMEOUT}с")
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("image too large")
                f.write(chunk)

        if size < MIN_IMAGE_BYTES:
            log.warning("Картинка подозрительно мелкая (%d байт) — пропускаем", size)
            with contextlib.suppress(OSError):
                os.remove(path)
            return None

        with open(path, "rb") as f:
            head = f.read(16)
        if not _looks_like_image(head):
            log.warning("Скачанные байты не похожи на изображение (magic bytes не совпали, "
                        "Content-Type=%s) — пропускаем: %s", mime, url[:100])
            with contextlib.suppress(OSError):
                os.remove(path)
            return None

        log.info("Картинка скачана: %d КБ, %s", size // 1024, mime)
        return path
    except Exception as e:  # noqa: BLE001 — скачивание картинки не должно ронять публикацию
        log.warning("Картинка недоступна (%s): %s", type(e).__name__, e)
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)
        return None
    finally:
        # close() сам может бросить (например, оборванное середина-потока
        # соединение) — это чистка, она не должна подменять собой уже
        # решённый результат функции (return/исключение выше).
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()


def mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for mime, e in EXT_BY_MIME.items():
        if e == ext and mime != "image/jpg":
            return mime
    return "image/jpeg"
