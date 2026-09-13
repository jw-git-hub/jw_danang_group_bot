#!/usr/bin/env python3
"""Скачивание заглавной картинки статьи для новостного поста.

Картинка нужна, чтобы новость выглядела статьёй, а не ссылкой с превью:
rich-сообщение Telegram умеет медиа-блок внутри текста, но превью ссылки
в нём не генерируется вовсе — значит изображение надо отдать явно.

Файл живёт на диске ровно столько, сколько идёт публикация: скачали → отправили
→ удалили. Для этого модуль даёт контекстный менеджер, который убирает файл
даже если отправка упала с исключением.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
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
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=DOWNLOAD_TIMEOUT, stream=True)
    except requests.exceptions.RequestException as e:
        log.warning("Картинка недоступна (%s): %s", type(e).__name__, e)
        return None

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
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    log.warning("Картинка превысила лимит %d байт при скачивании", MAX_IMAGE_BYTES)
                    raise ValueError("image too large")
                f.write(chunk)
    except (OSError, ValueError, requests.exceptions.RequestException) as e:
        log.warning("Скачивание картинки прервано: %s", e)
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    if size < MIN_IMAGE_BYTES:
        log.warning("Картинка подозрительно мелкая (%d байт) — пропускаем", size)
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    log.info("Картинка скачана: %d КБ, %s", size // 1024, mime)
    return path


def mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for mime, e in EXT_BY_MIME.items():
        if e == ext and mime != "image/jpg":
            return mime
    return "image/jpeg"
