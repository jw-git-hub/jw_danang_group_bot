"""Дайджесты-подборки: распознавание по заголовку и выбор картинки про Дананг.

Издания вроде Tuoi Tre выпускают «Vietnam Morning Monitor» — десяток новостей со
всей страны в одной публикации. og:image такой страницы относится к первому
сюжету подборки (пожар в Донгнае, подсолнухи в Зялае), а пост о ней пишется про
дананговский сюжет — иллюстрация получалась из чужой новости.

Модуль даёт две вещи: `is_digest()` — это подборка или обычная статья, и
`pick_local_image()` — первая картинка тела статьи, подпись которой говорит про
Дананг или Хойан. Если такой нет, вызывающий публикует без картинки: чужая
иллюстрация хуже отсутствия иллюстрации.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import urljoin

# Маркеры заголовка подборки. Слов weekly/highlights/headlines здесь нет
# намеренно: они дают ложные срабатывания на обычных статьях
# («2 weekly fire-and-water shows»).
DIGEST_TITLE_MARKERS = (
    r"\b(morning|evening|weekend) monitor\b",
    r"\bin brief\b",
    r"\bround-?up\b",
    r"\bdigest\b",
    r"\bnews today\b",
)
DIGEST_TITLE_RE = re.compile("|".join(DIGEST_TITLE_MARKERS), re.IGNORECASE)

# Дананг и Хойан (Хойан входит в город Дананг, поэтому подпись про него тоже
# годится) в латинской и вьетнамской записи, с пробелом и без; \s — потому что
# в подписях встречается неразрывный пробел («Da\xa0Nang»).
LOCAL_PLACE_RE = re.compile(
    r"\b(da\s?nang|đà\s?nẵng|hoi\s?an|hội\s?an)\b",
    re.IGNORECASE,
)

# Где искать картинки: сайдбары и «читайте также» вне тела статьи показывают
# чужие сюжеты. Берём первый найденный контейнер, иначе всю страницу.
ARTICLE_BODY_SELECTORS = (".detail-content", "article", "main")


def is_digest(title: str) -> bool:
    return bool(DIGEST_TITLE_RE.search(title))


def _find_article_body(soup):
    for selector in ARTICLE_BODY_SELECTORS:
        body = soup.select_one(selector)
        if body is not None:
            return body
    return soup


def _caption_mentions_local_place(figure) -> bool:
    caption = figure.find("figcaption")
    if caption is None:
        return False
    # Вьетнамские названия бывают в составной (NFD) записи: без нормализации
    # «Đà Nẵng» из «D + комбинирующие знаки» не совпал бы с шаблоном.
    text = unicodedata.normalize("NFC", caption.get_text(" ", strip=True))
    return bool(LOCAL_PLACE_RE.search(text))


def _figure_image_url(figure, page_url: str) -> Optional[str]:
    img = figure.find("img")
    if img is None:
        return None
    # При ленивой загрузке src — заглушка, настоящий адрес лежит в data-src
    raw_url = img.get("data-src") or img.get("src")
    if not raw_url:
        return None
    try:
        return urljoin(page_url, raw_url)
    except ValueError:
        # Мусорный адрес вида "http://[bad" — не должен ронять разбор страницы
        return None


def pick_local_image(soup, page_url: str) -> Optional[str]:
    """Абсолютный URL первой (в порядке документа) картинки тела статьи, в
    подписи которой упомянут Дананг или Хойан; None, если такой нет."""
    for figure in _find_article_body(soup).find_all("figure"):
        if not _caption_mentions_local_place(figure):
            continue
        image_url = _figure_image_url(figure, page_url)
        if image_url:
            return image_url
    return None
