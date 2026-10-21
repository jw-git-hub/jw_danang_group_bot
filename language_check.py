"""Поиск латиницы-«протечек» в русском тексте поста.

Модель иногда вставляет английские слова посреди русского текста
(«Они regularly заезжают…») или смешивает буквы двух алфавитов в одном слове
(«departamentов», «Мy»). Модуль находит такие фрагменты; допустимой латиницей
считаются названия, бренды и аббревиатуры (слово с заглавной буквой).
"""
from __future__ import annotations

import re

SOURCE_LINE_PREFIX = "📰 Источник:"
HASHTAG_LINE_PREFIX = "#"

LATIN_CLASS = "A-Za-zÀ-ÖØ-öø-ɏḀ-ỿ"
CYRILLIC_CLASS = "Ѐ-ӿ"

LATIN_CHAR_RE = re.compile(f"[{LATIN_CLASS}]")
CYRILLIC_CHAR_RE = re.compile(f"[{CYRILLIC_CLASS}]")
DIGIT_RE = re.compile(r"\d")

# Слово: буквы/цифры, внутри допустимы апостроф и дефис
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")
WORD_SEPARATOR_RE = re.compile(r"[ \t]*")
WORD_PART_SEPARATORS_RE = re.compile(r"['’\-]")

# Фрагменты, которые вырезаются до анализа
URL_RE = re.compile(r"https?://\S+")
DOMAIN_RE = re.compile(r"\b[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b")
HASHTAG_RE = re.compile(r"#\S+")
PARENTHESES_RE = re.compile(r"\([^()]*\)")
QUOTES_RE = re.compile(r"«[^»]*»|\"[^\"]*\"|“[^”]*”")
STRIPPED_PATTERNS = (URL_RE, DOMAIN_RE, HASHTAG_RE, PARENTHESES_RE, QUOTES_RE)

KIND_LATIN = "latin"
KIND_OTHER = "other"
KIND_MIXED = "mixed"


def _is_skipped_line(line: str) -> bool:
    """Строки источника и хэштегов не проверяются."""
    stripped = line.strip()
    return stripped.startswith(SOURCE_LINE_PREFIX) or stripped.startswith(HASHTAG_LINE_PREFIX)


def _strip_allowed_fragments(line: str) -> str:
    """Вырезает URL, домены, хэштеги, скобки и кавычки (заменяет пробелом)."""
    for pattern in STRIPPED_PATTERNS:
        line = pattern.sub(" ", line)
    return line


def _is_mixed_part(part: str) -> bool:
    return bool(LATIN_CHAR_RE.search(part) and CYRILLIC_CHAR_RE.search(part))


def _classify_word(word: str) -> str:
    """latin — чистая латиница; mixed — вплотную кириллица и латиница
    (дефис разделяет части, 'QR-код' не смесь); other — всё остальное."""
    if DIGIT_RE.search(word):
        return KIND_OTHER
    if any(_is_mixed_part(part) for part in WORD_PART_SEPARATORS_RE.split(word)):
        return KIND_MIXED
    if LATIN_CHAR_RE.search(word) and not CYRILLIC_CHAR_RE.search(word):
        return KIND_LATIN
    return KIND_OTHER


def _is_allowed_span(first_word: str) -> bool:
    """Бренд, название или аббревиатура: в первом слове есть заглавная буква."""
    return any(ch.isupper() for ch in first_word)


def _find_leaks_in_line(line: str) -> list[str]:
    """Смешанные слова и латинские спаны со строчным началом в одной строке."""
    leaks: list[str] = []
    span: list[str] = []
    previous_end = 0

    def flush_span() -> None:
        if span and not _is_allowed_span(span[0]):
            leaks.append(" ".join(span))
        span.clear()

    for match in WORD_RE.finditer(line):
        word = match.group()
        gap = line[previous_end:match.start()]
        previous_end = match.end()
        kind = _classify_word(word)
        if kind == KIND_LATIN and span and WORD_SEPARATOR_RE.fullmatch(gap) and gap:
            span.append(word)
            continue
        flush_span()
        if kind == KIND_MIXED:
            leaks.append(word)
        elif kind == KIND_LATIN:
            span.append(word)
    flush_span()
    return leaks


def find_latin_leaks(post_text: str) -> list[str]:
    """Подозрительные фрагменты латиницы без дублей, в порядке появления."""
    leaks: list[str] = []
    for line in post_text.split("\n"):
        if _is_skipped_line(line):
            continue
        for leak in _find_leaks_in_line(_strip_allowed_fragments(line)):
            if leak not in leaks:
                leaks.append(leak)
    return leaks
