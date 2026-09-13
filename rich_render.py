#!/usr/bin/env python3
"""Конвертер плоских постов проекта в Rich HTML (Telegram Bot API 10.1+).

Боты продолжают собирать пост как обычный текст — это остаётся источником
правды и рабочим форматом. Этот модуль поверх готового текста распознаёт
соглашения проекта и раскладывает их в rich-разметку:

    эмодзи + ЗАГОЛОВОК КАПСОМ   → <h3>
    абзац                        → <p>
    строки, начинающиеся с •     → <ul><li>
    "Подпись:" + абзац/буллеты   → <p><b>Подпись:</b></p> + содержимое
    🌴 Для экспатов: …           → <blockquote>
    📰 Источник: URL             → <footer> со ссылкой
    финальная строка хештегов    → <footer>

Так один конвертер покрывает все четыре рубрики (погода, новости, урок
вьетнамского, гайд экспата), а боты остаются нетронутыми.
"""
from __future__ import annotations

import html
import re

# Метки секций, которые оформляем как выделенную цитату
QUOTE_MARKERS = ("🌴 Для экспатов", "💡 На заметку")

# Метка источника
SOURCE_MARKER = "📰 Источник"

URL_RE = re.compile(r"https?://\S+")
# Строка-подпись секции: короткая, заканчивается двоеточием
LABEL_RE = re.compile(r"^(?P<label>.{1,60}:)\s*$")
# Эмодзи в начале строки — маркер заголовка независимо от регистра
# (эмотиконы/пиктограммы, флаги-регионалы, стрелки, дингбаты, VS16).
EMOJI_RE = re.compile(
    "^["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "☀-➿"
    "←-⇿"
    "⬀-⯿"
    "️"
    "]"
)


def _esc(text: str) -> str:
    """Экранирование под Rich HTML (те же <, >, & что и в обычном режиме)."""
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    """Экранирует текст и оборачивает голые URL в <a>.

    Порядок принципиален: URL ищем и обрезаем от хвостовой пунктуации
    (".,;)") в СЫРОМ тексте, а не в уже экранированном — иначе rstrip
    откусывает символ у готовой HTML-сущности (например ";" у "&amp;"
    вместо ";" у исходного "&"), и URL с UTM-хвостом вида "...?a=1&"
    разваливается: href="...&amp" и лишняя ";" в видимом тексте.

    href экранируется ОТДЕЛЬНО от видимого текста и обязательно с
    quote=True: значение подставляется внутрь атрибута href="{...}", а
    контент новостей приходит от claude поверх заголовков сторонних
    сайтов — то есть URL не полностью доверенный. Без quote=True двойная
    кавычка в URL доживает до атрибута (инъекция в href) и в любом случае
    рвёт разметку — Telegram отвечает 400 "can't parse entities".
    """
    out: list[str] = []
    last = 0
    for m in URL_RE.finditer(text):
        out.append(_esc(text[last:m.start()]))
        url = m.group(0).rstrip(".,;)")
        tail = m.group(0)[len(url):]
        href = html.escape(url, quote=True)
        out.append(f'<a href="{href}">{_esc(url)}</a>')
        out.append(_esc(tail))
        last = m.end()
    out.append(_esc(text[last:]))
    return "".join(out)


def _is_title(line: str, *, block_is_single_line: bool = False) -> bool:
    """Первая строка вида «🇻🇳 УРОК ВЬЕТНАМСКОГО — День 91 / 365» — капсом,
    либо (только если это единственная строка своего блока — чтобы не
    задеть первую строку многострочного абзаца/прогноза, которая тоже может
    начинаться с эмодзи) короткий нейтральный по регистру заголовок вида
    «📰 Открылся новый ресторан», начинающийся с эмодзи. Без второй ветки
    заголовок не капсом молча не распознаётся: теряется не только <h3>, но
    и <hr/> перед хвостом поста, потому что вся строка уходит в обычный
    текст ленты."""
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 4:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.7:
            return True
    if block_is_single_line:
        stripped = line.strip()
        if stripped and len(stripped) < 90 and EMOJI_RE.match(stripped):
            return True
    return False


def _is_hashtags(block: str) -> bool:
    tokens = block.split()
    return bool(tokens) and all(t.startswith("#") for t in tokens)


def _render_bullets(lines: list[str]) -> str:
    # Сначала снимаем внешние пробелы, потом маркер «•», потом снова пробелы:
    # у вложенного пункта вида "  • вложенный" ведущий пробел стоит ПЕРЕД
    # маркером, и lstrip('•') по нему не срабатывает (первый символ — не
    # '•'), поэтому сам маркер раньше протекал в текст пункта нетронутым.
    items = "".join(
        f"<li>{_inline(l.strip().lstrip('•').strip())}</li>" for l in lines
    )
    return f"<ul>{items}</ul>"


def _render_block(block: str) -> str:
    lines = [l for l in block.split("\n") if l.strip()]
    if not lines:
        return ""

    if _is_hashtags(block):
        return f"<footer>{_inline(block.strip())}</footer>"

    if block.lstrip().startswith(SOURCE_MARKER):
        return f"<footer>{_inline(block.strip())}</footer>"

    for marker in QUOTE_MARKERS:
        if block.lstrip().startswith(marker):
            return f"<blockquote>{_inline(block.strip())}</blockquote>"

    # «Подпись:» отдельной строкой, дальше содержимое секции
    m = LABEL_RE.match(lines[0])
    if m and len(lines) > 1:
        head = f"<p><b>{_inline(m.group('label'))}</b></p>"
        rest = lines[1:]
        if all(l.lstrip().startswith("•") for l in rest):
            return head + _render_bullets(rest)
        return head + "".join(f"<p>{_inline(l)}</p>" for l in rest)

    if all(l.lstrip().startswith("•") for l in lines):
        return _render_bullets(lines)

    # Обычный абзац: переносы внутри блока значимы (прогноз погоды, разбор)
    return "<p>" + "<br/>".join(_inline(l) for l in lines) + "</p>"


def plain_to_rich_html(text: str, *, heading_size: int = 3) -> str | None:
    """Главная точка входа: плоский пост → строка Rich HTML.

    На пустом или состоящем из пробелов входе возвращает None (а не пустую
    строку) — пустое тело гарантированно даёт 400 при отправке в Telegram,
    поэтому вызывающий ОБЯЗАН проверить результат на None перед отправкой.
    """
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if not blocks:
        return None

    out: list[str] = []

    first_lines = blocks[0].split("\n")
    if _is_title(first_lines[0], block_is_single_line=len(first_lines) == 1):
        out.append(f"<h{heading_size}>{_inline(first_lines[0].strip())}</h{heading_size}>")
        remainder = "\n".join(first_lines[1:]).strip()
        rest_blocks = ([remainder] if remainder else []) + blocks[1:]
    else:
        rest_blocks = blocks

    # Разделитель перед хвостом поста (источник / хештеги), если он есть
    tail_start = None
    for i, b in enumerate(rest_blocks):
        if _is_hashtags(b) or b.lstrip().startswith(SOURCE_MARKER):
            tail_start = i
            break

    for i, block in enumerate(rest_blocks):
        if i == tail_start:
            out.append("<hr/>")
        out.append(_render_block(block))

    return "".join(out)


def extract_source_url(text: str) -> str | None:
    """URL из строки «📰 Источник: …» — для link_preview_options.url."""
    for line in text.split("\n"):
        if line.lstrip().startswith(SOURCE_MARKER):
            m = URL_RE.search(line)
            if m:
                return m.group(0).rstrip(".,;)")
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,;)") if m else None
