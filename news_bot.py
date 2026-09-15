#!/usr/bin/env python3
"""
Автопостинг новостей о Дананге в Telegram.
Ищет свежие новости через Google News RSS, переводит через Claude Code CLI,
отправляет в Telegram с фото. Требует установленный claude (Claude Code).

Запуск: python3 news_bot.py [--test] [--force] [--init]
Cron:
  0 9 * * *  cd /path/to/danang-bots && python3 news_bot.py >> logs/news.log 2>&1
  0 19 * * * cd /path/to/danang-bots && python3 news_bot.py >> logs/news.log 2>&1
"""
from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import new_decoderv1

from dedup import is_duplicate, extract_fingerprint, _strip_publisher_suffix
from dedup import load_tracker as _dedup_load_tracker
from dedup import save_tracker as _dedup_save_tracker
from dedup import acquire_lock, tracker_lock_path as _dedup_lock_path
from telegram_sender import send_rich_message, send_telegram_message, SendOutcomeUnknown
from rich_render import plain_to_rich_html
from article_image import downloaded_image, mime_for

# googlenewsdecoder делает requests.get/post без timeout — при недоступности
# или троттлинге Google одно подвисшее соединение способно повесить весь
# прогон. Атрибут пакета googlenewsdecoder.new_decoderv1 — это сама функция
# decode_google_news_url, а не модуль; настоящий модуль с его собственным
# `requests`, которым она фактически пользуется, зарегистрирован в
# sys.modules под тем же путём — патчим requests именно там.
_new_decoderv1_module = sys.modules.get("googlenewsdecoder.new_decoderv1")
if _new_decoderv1_module is not None:
    _new_decoderv1_module.requests = SimpleNamespace(
        get=functools.partial(requests.get, timeout=15),
        post=functools.partial(requests.post, timeout=15),
        exceptions=requests.exceptions,
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Config not found: %s", CONFIG_PATH)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Config JSON error: %s", e)
        sys.exit(1)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# --test:  слать в тестовый чат, трекер не трогать (существующий флаг)
# --force: игнорировать закрытое окно расписания (существующий флаг)
# --init:  разрешить старт с пустым трекером, если файла ещё нет (новый флаг,
#          для первого запуска на новом сервере; иначе см. load_tracker)
ALLOWED_ARGS = {"--test", "--force", "--init"}


def check_argv():
    """Неизвестный аргумент — это почти всегда опечатка в cron/systemd unit,
    которую молча проглатывать нельзя: раньше любой мусор в argv просто
    игнорировался, и опечатавшийся флаг никак не давал о себе знать."""
    unknown = [a for a in sys.argv[1:] if a not in ALLOWED_ARGS]
    if unknown:
        print(
            f"Usage: {sys.argv[0]} [--test] [--force] [--init]\n"
            f"Unknown argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        sys.exit(2)

# ---------------------------------------------------------------------------
# Tracker (deduplication) — единая реализация в dedup.py, см. её докстринг.
# ---------------------------------------------------------------------------
def load_tracker(cfg, allow_init=False):
    """Отсутствие файла трекера — не повод тихо начинать с пустого места
    (кроме явного --init или read_history.py, который как раз восстанавливает
    трекер из истории треда): иначе бот перепостил бы всю историю заново."""
    tracker_path = Path(__file__).parent / cfg["news"]["tracker_file"]
    if not tracker_path.exists() and not allow_init:
        log.error(
            "трекер не найден: перенесите danang-news-posted.json со старого "
            "сервера, запустите read_history.py или передайте --init для "
            "старта с пустым трекером"
        )
        sys.exit(1)
    return _dedup_load_tracker(tracker_path), tracker_path


def save_tracker(tracker, tracker_path):
    _dedup_save_tracker(tracker_path, tracker)

# ---------------------------------------------------------------------------
# Window check (morning 9-18, evening 19-23) — Danang time UTC+7
# ---------------------------------------------------------------------------
DANANG_TZ = timezone(timedelta(hours=7))

def get_current_window():
    now = datetime.now(DANANG_TZ)
    window = "morning" if now.hour < 18 else "evening"
    today = now.strftime("%Y-%m-%d")
    return f"{today}_{window}"


def is_window_closed(tracker, window_key):
    return window_key in tracker.get("windows", {})

# ---------------------------------------------------------------------------
# News search (Google News RSS)
# ---------------------------------------------------------------------------
SEARCH_QUERIES = [
    '"Da Nang" OR "Danang" news today',
    '"Da Nang" OR "Danang" expat OR tourism OR travel',
    'site:e.thedanangnews.vn',
    'site:vietnamnews.vn Da Nang',
    'site:vnexpress.net/english danang',
    'site:tuoitrenews.vn Da Nang OR Danang',
    'site:dtinews.dantri.com.vn Da Nang',
    'site:vietnam.vn/en Da Nang',
    'site:en.nhandan.vn Da Nang OR Danang',
    'site:thanhniennews.com Da Nang',
    'site:saigoneer.com Da Nang OR Danang',
    'site:vir.com.vn Da Nang OR Danang',
    'site:hanoitimes.vn Da Nang',
    'site:en.baodanang.vn',
    'Vietnam visa immigration policy Da Nang',
    'Da Nang infrastructure development 2026',
    'central Vietnam tourism Da Nang',
]

PRIORITY_DOMAINS = [
    "e.thedanangnews.vn",
    "en.baodanang.vn",
    "vietnamnews.vn",
    "vnexpress.net",
    "tuoitrenews.vn",
    "dtinews.dantri.com.vn",
    "news.tuoitre.vn",
    "en.nhandan.vn",
    "thanhniennews.com",
    "saigoneer.com",
    "vir.com.vn",
    "hanoitimes.vn",
    "vietnam.vn",
]


def search_google_news(query, max_results=10):
    """Search Google News RSS for articles."""
    encoded_q = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en&gl=VN&ceid=VN:en"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DanangNewsBot/1.0)"}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Google News RSS failed for '%s': %s", query, e)
        return []

    articles = []
    try:
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item")[:max_results]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            source = item.findtext("source", "")

            # Google News wraps links — try to extract the real URL
            real_url = resolve_google_news_url(link)

            articles.append({
                "title": title,
                "url": real_url or link,
                "pub_date": pub_date,
                "source": source,
            })
    except ET.ParseError as e:
        log.warning("XML parse error: %s", e)

    return articles


def _is_external_url(url):
    """Return True if URL is not a Google domain."""
    try:
        host = urlparse(url).netloc.lower()
        return host and "google" not in host and "gstatic" not in host
    except Exception:
        return False


def resolve_google_news_url(google_url):
    """Try to resolve a Google News redirect URL to the real article URL.

    Uses multiple strategies:
    1. googlenewsdecoder library (uses Google's internal batchexecute API)
    2. HEAD redirect (fast, works occasionally)
    3. GET with redirect following
    """
    if "news.google.com" not in google_url:
        return google_url

    # Strategy 1: googlenewsdecoder (most reliable for current Google News URLs)
    try:
        result = new_decoderv1(google_url)
        if result.get("status") and result.get("decoded_url"):
            decoded = result["decoded_url"]
            log.debug("Resolved via googlenewsdecoder: %s", decoded)
            return decoded
    except Exception as e:
        log.debug("googlenewsdecoder failed for %s: %s", google_url, e)

    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

    # Strategy 2: HEAD redirect (fast, works occasionally)
    try:
        resp = requests.head(google_url, allow_redirects=True, timeout=10, headers=ua)
        if _is_external_url(resp.url):
            log.debug("Resolved via HEAD redirect: %s", resp.url)
            return resp.url
    except Exception:
        pass

    # Strategy 3: GET with redirect following
    try:
        resp = requests.get(google_url, allow_redirects=True, timeout=15, headers=ua)
        if _is_external_url(resp.url):
            log.debug("Resolved via GET redirect: %s", resp.url)
            return resp.url
    except Exception:
        pass

    log.debug("Could not resolve Google News URL: %s", google_url)
    return google_url


def _parse_pub_date(pub):
    """Разбирает дату публикации: RFC-2822 (формат Google News RSS) или
    ISO-8601. Наивные datetime считаются UTC (см. C4 — Python 3.9 не понимает
    суффикс "Z" в fromisoformat, приводим его к явному смещению вручную).
    Возвращает aware datetime либо None, если строка не распознана ни в одном
    формате — тогда статью нельзя проверить по возрасту, и она пропускается
    (см. filter_articles), а не публикуется вслепую."""
    if not pub:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(pub)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        iso = pub.strip()
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def score_article(article):
    """Score an article by source priority and freshness."""
    score = 0
    url = article["url"]
    domain = urlparse(url).netloc.replace("www.", "")

    # Priority source bonus
    for i, d in enumerate(PRIORITY_DOMAINS):
        if d in domain:
            score += (len(PRIORITY_DOMAINS) - i) * 10
            break

    # Freshness bonus (rough check from pub_date). Сравниваем с UTC-«сейчас»,
    # а не datetime.now(dt.tzinfo): при наивном dt (см. _parse_pub_date) это
    # означало бы локальное время СЕРВЕРА, а не UTC — на сервере не в UTC
    # возраст статьи считался бы со сдвигом на локальный часовой пояс.
    dt = _parse_pub_date(article.get("pub_date", ""))
    if dt is not None:
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age_hours < 12:
            score += 30
        elif age_hours < 24:
            score += 20
        elif age_hours < 48:
            score += 10

    return score


def filter_articles(articles, tracker, cfg):
    """Filter out duplicates, old/undated news, and sort by score."""
    max_age = timedelta(days=cfg["news"]["max_age_days"])
    threshold = cfg["news"]["dedup_threshold"]
    filtered = []

    for art in articles:
        # Skip duplicates
        if is_duplicate(art["url"], art["title"], tracker, threshold):
            continue

        # Дату не удалось разобрать вовсе — раньше это означало "не проверяем
        # возраст" и статья могла быть сколь угодно старой. Без даты возраст
        # не проверить, поэтому пропускаем статью, а не публикуем вслепую.
        dt = _parse_pub_date(art.get("pub_date", ""))
        if dt is None:
            log.debug("Нет распознаваемой даты публикации, пропускаем: %s", art["title"][:60])
            continue
        if datetime.now(timezone.utc) - dt > max_age:
            log.debug("Too old: %s", art["title"][:60])
            continue

        art["score"] = score_article(art)
        filtered.append(art)

    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
    return filtered

# ---------------------------------------------------------------------------
# Article extraction (OG image + text)
# ---------------------------------------------------------------------------
def extract_article_meta(url):
    """Fetch article page and extract OG image and description."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DanangNewsBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        og_image = None
        og_desc = None
        og_title = None

        # OG image
        meta_img = soup.find("meta", property="og:image")
        if meta_img:
            og_image = meta_img.get("content")

        # OG description
        meta_desc = soup.find("meta", property="og:description")
        if meta_desc:
            og_desc = meta_desc.get("content")

        # OG title
        meta_title = soup.find("meta", property="og:title")
        if meta_title:
            og_title = meta_title.get("content")

        # Fallback image: first article img
        if not og_image:
            for selector in ["article img", ".content img", "main img", ".post img"]:
                img = soup.select_one(selector)
                if img and img.get("src"):
                    og_image = img["src"]
                    break

        # Extract article text for Claude
        article_text = ""
        for tag in soup.select("article p, .content p, main p, .detail-content p"):
            article_text += tag.get_text(strip=True) + "\n"

        if not article_text:
            # Fallback: all paragraphs
            for p in soup.find_all("p"):
                text = p.get_text(strip=True)
                if len(text) > 50:
                    article_text += text + "\n"

        return {
            "og_image": og_image,
            "og_description": og_desc,
            "og_title": og_title,
            "article_text": article_text[:5000],  # Limit for Claude context
        }
    except Exception as e:
        log.warning("Failed to extract article meta from %s: %s", url, e)
        return {"og_image": None, "og_description": None, "og_title": None, "article_text": ""}

# ---------------------------------------------------------------------------
# Claude Code CLI — translate & format post
# ---------------------------------------------------------------------------
# Пересказ новости по шаблону — Sonnet достаточно; без явной модели claude -p берёт Opus из глобальных настроек и зря тратит лимиты
CLAUDE_MODEL = "sonnet"

CLAUDE_PROMPT = """Ты — редактор новостного Telegram-канала для русскоязычных экспатов в Дананге, Вьетнам.

Напиши пост на РУССКОМ языке по этой англоязычной новости. Строго следуй формату:

[2-3 эмодзи] ЗАГОЛОВОК КАПСОМ [1-2 эмодзи]

[эмодзи] Первый абзац: суть новости в 2-3 предложениях.

[эмодзи] Второй абзац: ключевые детали, цифры, факты.

[эмодзи] Третий абзац: дополнительные подробности или контекст.

[эмодзи] Четвёртый абзац (если нужен): хронология или предыстория.

🌴 Для экспатов: как эта новость влияет на жизнь иностранцев в Дананге/Вьетнаме. 1-2 предложения.

📰 Источник: {url}

#Дананг #Danang #жизньвДананге #экспатДананг #[тема] #Vietnam{year}

Правила:
- Живой русский язык, не калька с английского
- Каждый абзац начинается с тематического эмодзи
- Обязательно абзац «Для экспатов» с эмодзи 🌴
- Обязательно строка «📰 Источник: URL» — URL не менять
- Ровно 5 хэштегов: первые 4 всегда #Дананг #Danang #жизньвДананге #экспатДананг, пятый по теме
- Последний хэштег #Vietnam{year} (итого 6)
- Никакого Markdown (* ** _)
- Пустая строка между абзацами
- НЕ добавляй ссылку на Telegram-группу
- Выведи ТОЛЬКО текст поста. Начни сразу с эмодзи и заголовка. Без преамбул, комментариев, пояснений, вступлений типа «Вот пост:». Никаких мета-комментариев до или после поста
- Если текст статьи не совпадает с заголовком — пиши по заголовку и доступной информации, НЕ отказывайся и НЕ пиши пояснения

Заголовок статьи: {title}

Текст статьи:
{text}
"""

# Диапазоны эмодзи (см. _is_emoji_char) — дополняются unicodedata, но
# оставлены явно: категория "So" не покрывает часть Dingbats/стрелок, уже
# использовавшихся в реальных постах.
_EMOJI_RANGES = [
    (0x1F300, 0x1FAFF),  # Misc Symbols, Emoticons, etc.
    (0x2600, 0x27BF),    # Misc symbols, Dingbats
    (0x2700, 0x27BF),    # Dingbats
    (0xFE00, 0xFE0F),    # Variation selectors
    (0x1F900, 0x1F9FF),  # Supplemental Symbols
    (0x231A, 0x23FA),    # Watch, hourglass, etc.
    (0x25AA, 0x25FE),    # Geometric shapes
    (0x2B05, 0x2B55),    # Arrows, circles
]

# Реплики/отказы модели — то, что могло попасть в вывод claude -p ДО или
# ПОСЛЕ самого поста, а иногда и на одной строке с заголовком. Список
# используется и для отсечения преамбулы (см. clean_ai_output), и как
# финальная проверка в generate_post (см. ниже).
_OPENER_CHATTER_PATTERN = r'^\W*(вот|here|готово|конечно|sure|если нужно|let me|надеюсь|могу)\b'
CHATTER_RE = re.compile(
    r'^(Примечание|Note|P\.S\.|Комментарий|Пояснение)\s*:'
    r'|я не могу|мне нужно|не соответствует|I cannot|I can\'t|предоставленный текст'
    r'|' + _OPENER_CHATTER_PATTERN,
    re.IGNORECASE,
)
# Финальная проверка в generate_post (см. ниже) — уже, чем CHATTER_RE (без
# отказных фраз) и, в отличие от очистки, без исключения для строк с эмодзи:
# если что-то похожее на реплику модели дожило до конца очистки, пост лучше
# отклонить целиком, чем опубликовать с остатком "болтовни".
OPENER_CHATTER_RE = re.compile(_OPENER_CHATTER_PATTERN, re.IGNORECASE)


def _is_emoji_char(ch):
    if not ch:
        return False
    if unicodedata.category(ch) == "So":
        return True
    cp = ord(ch)
    return any(start <= cp <= end for start, end in _EMOJI_RANGES)


def _starts_with_emoji(text):
    """Check if text starts with an emoji character.

    ch — первый символ после Markdown-обёртки (**/_) и пробелов/табов:
    модель иногда оборачивает заголовок в разметку или ставит его после
    преамбулы на той же строке. unicodedata.category(ch) == "So" покрывает
    символьные эмодзи одним кодпоинтом, включая флаги (🇻🇳) и "квадратные"
    символы (🆕, 🆘), которые не попадали в старые числовые диапазоны.
    """
    if not text:
        return False
    ch = text.lstrip("*_ \t")[:1]
    return _is_emoji_char(ch)


def _find_emoji_pos(line):
    """Позиция первого эмодзи-символа в строке, -1 если его нет."""
    for i, ch in enumerate(line):
        if _is_emoji_char(ch):
            return i
    return -1


def _is_caps_title(line):
    """≥70% заглавных букв среди буквенных символов строки — тот же порог,
    которым _validate_post проверяет заголовок поста (единая реализация для
    обоих мест). Заголовок-капслок, случайно совпавший с CHATTER_RE (например,
    содержащий «НЕ СООТВЕТСТВУЕТ» или начинающийся с «ВОТ ПОЧЕМУ»), — это
    почти наверняка настоящий заголовок статьи, а не реплика модели: репликами
    капслоком не пишут."""
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.7


def clean_ai_output(text):
    """Remove AI meta-commentary from Claude's output, keeping only the post."""
    lines = text.split('\n')

    # Find the start: первая строка, где эмодзи есть и она не похожа на
    # реплику модели. Если эмодзи не в начале строки — значит перед ним
    # преамбула на той же строке ("Вот пост: 🚢 ЗАГОЛОВОК") — отрезаем её,
    # а не выбрасываем строку целиком (иначе потерялся бы сам заголовок).
    # Если найденная строка САМА оказалась репликой ("✅ Готово! Вот пост
    # для канала:" — тоже начинается с эмодзи) — пропускаем её и ищем дальше.
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        pos = _find_emoji_pos(stripped)
        if pos == -1:
            continue
        candidate = stripped[pos:].rstrip("*_ \t")
        # Markdown-обёртка (**bold**/__underline__) может остаться вокруг текста
        # ПОСЛЕ эмодзи («🚗 **VINFAST ОТКРЫЛА ЗАВОД**» — сам эмодзи вне обёртки,
        # поэтому rstrip выше её не достаёт). Строка здесь — заголовок поста,
        # чистим именно её; остальные строки своей разметкой не трогаем.
        candidate = candidate.replace("**", "").replace("__", "")
        if CHATTER_RE.search(candidate) and not _is_caps_title(candidate):
            continue
        lines[i] = candidate
        start_idx = i
        break
    if start_idx is None:
        start_idx = 0

    # Find the end: last line containing the year hashtag (#VietnamNNNN) —
    # запасной вариант "#Vietnam" без цифр на случай, если модель ошиблась
    # с годом.
    end_idx = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        if re.search(r'#Vietnam\d*\b', lines[i]):
            end_idx = i
            break

    # Extract the post
    post_lines = lines[start_idx:end_idx + 1]

    # Filter out any remaining meta-commentary lines within the post — но
    # только те, что НЕ начинаются с эмодзи: реальный абзац поста может
    # содержать похожие на отказ слова (например, "не соответствует нормам"
    # про качество воды), и его нужно сохранить.
    cleaned = [
        line for line in post_lines
        if not (line.strip() and not _starts_with_emoji(line.strip()) and CHATTER_RE.search(line.strip()))
    ]

    return '\n'.join(cleaned).strip()


def _validate_post(post_text, article, year):
    """Возвращает None, если пост прошёл все проверки формата, иначе — текст
    причины отказа (для лога). Отказ значит "пробуем следующего кандидата" —
    никаких попыток доправить текст руками или обрезать по лимиту здесь нет,
    это отдельная (и куда более частая) причина потерять заголовок или
    хвост поста, чем реальная нехватка кандидатов."""
    lines = [l for l in post_text.split("\n") if l.strip()]
    if not lines:
        return "пустой"

    first_line = lines[0].strip()
    if not _is_caps_title(first_line):
        return f"заголовок не капсом (<70% заглавных букв в первой строке): {first_line[:80]!r}"

    if "🌴 Для экспатов:" not in post_text:
        return "нет абзаца '🌴 Для экспатов:'"

    source_line = f"📰 Источник: {article['url']}"
    if not any(l.strip() == source_line for l in lines):
        return f"нет строки {source_line!r}"

    last_line = lines[-1].strip()
    year_tag = f"#Vietnam{year}"
    if not last_line.endswith(year_tag):
        return f"последняя строка не заканчивается на {year_tag}: {last_line[:80]!r}"

    for line in lines:
        s = line.strip()
        if not _starts_with_emoji(s) and OPENER_CHATTER_RE.search(s):
            return f"похоже на реплику модели: {line[:80]!r}"

    if len(post_text) > 4000:
        return f"слишком длинный ({len(post_text)} символов > 4000) — отклоняем, а не обрезаем"

    return None


def generate_post(cfg, article, meta):
    """Use Claude Code CLI (claude -p) to translate and format the news post."""
    article_text = meta.get("article_text") or meta.get("og_description") or article["title"]
    year = datetime.now(DANANG_TZ).year

    prompt = CLAUDE_PROMPT.format(
        url=article["url"],
        title=article["title"],
        text=article_text,
        year=year,
    )

    try:
        result = subprocess.run(
            # --strict-mcp-config и --tools "" — ПОСЛЕДНИМИ аргументами, ничего
            # позиционного после --tools "" быть не должно. В prompt попадает
            # текст статьи с внешнего сайта (недоверенный ввод), а пользовательские
            # настройки Claude Code разрешают Bash(python3 -c ...) — без
            # отключения инструментов промпт-инъекция из статьи получила бы
            # возможность что-то выполнить. cwd — вне репозитория: с
            # отключёнными инструментами это уже не должно быть нужно, но так
            # claude точно не окажется в директории с config.json и сессией.
            ["claude", "-p", prompt, "--model", CLAUDE_MODEL, "--strict-mcp-config", "--tools", ""],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
        )

        if result.returncode != 0:
            # Claude CLI пишет диагностику (лимиты, авторизация) в stdout, а не в
            # stderr. Раньше логировался только stderr — и 67 отказов подряд, из
            # которых сложилась месячная тишина, не оставили в логе ни одной
            # строки с причиной.
            log.error("Claude Code CLI error (rc=%d)\n  stdout: %s\n  stderr: %s",
                      result.returncode,
                      (result.stdout or "")[:1000].strip() or "<пусто>",
                      (result.stderr or "")[:500].strip() or "<пусто>")
            return None

        post_text = result.stdout.strip()
        post_text = clean_ai_output(post_text)

        # Log stderr even on success
        if result.stderr:
            log.warning("Claude stderr (rc=0): %s", result.stderr[:500])

        log.info("Claude generated post: %d chars", len(post_text))
        log.debug("Post preview: %.200s", post_text)

        fail_reason = _validate_post(post_text, article, year)
        if fail_reason:
            log.error("Пост не прошёл проверку формата (%s). Preview: %.200s", fail_reason, post_text)
            return None

        return post_text
    except subprocess.TimeoutExpired:
        log.error("Claude Code CLI timed out (120s)")
        return None
    except FileNotFoundError:
        log.error("'claude' command not found. Install Claude Code: https://docs.anthropic.com/en/docs/claude-code")
        return None
    except Exception as e:
        log.error("Claude Code error: %s", e)
        return None

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# Сколько статей пробуем, если Claude не смог сделать пост из первой
MAX_CANDIDATE_ATTEMPTS = 3

def main():
    # Разбор argv — первым делом, до любого лока/сети/чтения состояния (см. C2):
    # опечатавшийся флаг из cron/systemd unit должен провалить запуск сразу и
    # явно, а не быть молча проигнорирован где-то на середине работы.
    check_argv()

    log.info("=== News bot start ===")
    # --force игнорирует окно расписания: нужен для тестовых прогонов и ручной
    # публикации, когда штатное окно уже закрыто.
    force = "--force" in sys.argv
    allow_init = "--init" in sys.argv
    cfg = load_config()

    # news_bot.py и read_history.py читают/пишут один и тот же tracker_file —
    # без общего лока конкурентный запуск (например, ручной прогон поверх
    # ещё не завершившегося cron) мог бы задвоить публикацию или потерять
    # запись при сохранении. Лок берём ДО загрузки трекера (как read_history.py),
    # а не после — иначе окно между чтением файла и захватом лока остаётся
    # незащищённым от гонки.
    tracker_path = Path(__file__).parent / cfg["news"]["tracker_file"]
    _lock_fh = acquire_lock(_dedup_lock_path(tracker_path))  # noqa: F841 — держим ссылку, чтобы лок не снялся раньше времени

    # Load tracker (см. C2 про политику отсутствующего файла)
    tracker, tracker_path = load_tracker(cfg, allow_init=allow_init)

    # Check window
    window_key = get_current_window()
    if is_window_closed(tracker, window_key):
        if not force:
            log.info("Window %s already closed — skipping.", window_key)
            return
        log.info("Window %s закрыто, но задан --force — продолжаем.", window_key)

    # Search for news
    log.info("Searching for news...")
    all_articles = []
    for query in SEARCH_QUERIES:
        articles = search_google_news(query, max_results=8)
        all_articles.extend(articles)
        log.info("  '%s' → %d articles", query, len(articles))

    # Deduplicate search results by URL
    seen_urls = set()
    unique_articles = []
    for art in all_articles:
        if art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            unique_articles.append(art)

    log.info("Total unique articles: %d", len(unique_articles))

    # URL уже резолвится внутри search_google_news (см. resolve_google_news_url).
    # Раньше здесь был ВТОРОЙ проход resolve_google_news_url по тем же URL —
    # если первая попытка не удавалась, повтор шёл по той же цепочке
    # decoder→HEAD→GET и просто удваивал число сетевых запросов, почти
    # никогда не добавляя успеха. Ссылки, для которых резолв так и не
    # удался (всё ещё news.google.com), пропускаем: постить редирект-страницу
    # Google вместо статьи нельзя — не тот og:image/текст и не тот
    # канонический URL для дедупа.
    still_google = sum(1 for a in unique_articles if "news.google.com" in a["url"])
    if still_google:
        log.info("Пропущено %d статей — не удалось резолвить ссылку Google News", still_google)
    unique_articles = [a for a in unique_articles if "news.google.com" not in a["url"]]
    log.info("After URL resolution: %d unique articles", len(unique_articles))

    # Filter and rank
    candidates = filter_articles(unique_articles, tracker, cfg)
    log.info("Candidates after filtering: %d", len(candidates))

    if not candidates:
        # Ненулевой код: «новостей не нашлось» неотличимо от «сеть легла» или
        # «дедуп выбросил всё», а тихий выход с нулём три месяца прятал такие
        # отказы от любого мониторинга.
        log.error("Подходящих статей не найдено — публикации не будет")
        sys.exit(1)

    # Перебираем несколько кандидатов: раньше пробовался ровно один, и если
    # Claude на нём спотыкался, запуск терялся целиком, хотя в списке лежало
    # ещё несколько подходящих статей.
    article = None
    meta = None
    post_text = None
    for candidate in candidates[:MAX_CANDIDATE_ATTEMPTS]:
        log.info("Selected: '%s' (score=%d)", candidate["title"][:80], candidate.get("score", 0))
        log.info("URL: %s", candidate["url"])

        log.info("Extracting article metadata...")
        candidate_meta = extract_article_meta(candidate["url"])
        log.info("OG image: %s",
                 candidate_meta["og_image"][:100] if candidate_meta["og_image"] else "None")

        log.info("Generating post with Claude...")
        candidate_text = generate_post(cfg, candidate, candidate_meta)
        if candidate_text:
            article, meta, post_text = candidate, candidate_meta, candidate_text
            break
        log.warning("Кандидат не дал поста — пробуем следующий")

    if not post_text:
        log.error("Ни один из %d кандидатов не дал валидный пост. Aborting.",
                  min(len(candidates), MAX_CANDIDATE_ATTEMPTS))
        sys.exit(1)

    # Публикуем новость как статью: rich-сообщение с картинкой внутри. Превью
    # ссылки в rich не генерируется — поэтому изображение отдаём явно, скачивая его во
    # временный файл, который удаляется сразу после отправки.
    #
    # send_* поднимает SendOutcomeUnknown, если исход отправки неизвестен
    # (таймаут ответа, обрыв соединения, 5xx, непарсящийся ответ) — запрос
    # мог реально дойти до Telegram. В этом случае ничего больше не шлём
    # (ни повтор, ни fallback): рискуем задвоить пост. Фоллбэки (rich без
    # фото, затем обычное сообщение) пробуются только после ТОЧНОГО отказа
    # (функция вернула None).
    test_mode = "--test" in sys.argv
    thread_id = cfg["telegram"].get("news_thread_id")
    rich_html = plain_to_rich_html(post_text)
    msg_id = None
    uncertain = False
    had_photo = False

    if rich_html:
        with downloaded_image(meta.get("og_image")) as image_path:
            had_photo = image_path is not None
            try:
                msg_id = send_rich_message(
                    cfg, rich_html, thread_id=thread_id, test=test_mode, rubric="news",
                    photo_path=image_path,
                    photo_mime=mime_for(image_path) if image_path else "image/jpeg",
                )
            except SendOutcomeUnknown as e:
                log.error(
                    "Rich-статья%s: исход отправки не подтверждён (%s) — дальше "
                    "не пробуем, чтобы не задвоить пост",
                    " (с фото)" if had_photo else "", e,
                )
                uncertain = True

        if msg_id is None and not uncertain and had_photo:
            # Возможно, дело было именно в фото — пробуем ту же rich-статью
            # ещё раз, но уже без него, прежде чем откатываться на обычное
            # сообщение.
            log.warning("Rich-статья с фото не ушла — пробуем без фото")
            try:
                msg_id = send_rich_message(
                    cfg, rich_html, thread_id=thread_id, test=test_mode, rubric="news",
                )
            except SendOutcomeUnknown as e:
                log.error(
                    "Rich-статья (без фото): исход отправки не подтверждён (%s) — "
                    "дальше не пробуем, чтобы не задвоить пост", e,
                )
                uncertain = True

        if msg_id is None and not uncertain:
            log.warning("Rich-статья не ушла — откатываемся на обычный пост с превью")

    if msg_id is None and not uncertain:
        # Откат: обычное сообщение. Картинка тогда приходит превью из ссылки, но
        # крупной она станет только при ЯВНОМ link_preview_options.url — без него
        # Telegram игнорирует prefer_large_media и рисует иконку сбоку.
        try:
            msg_id = send_telegram_message(
                cfg,
                post_text,
                thread_id=thread_id,
                link_preview_url=article["url"],
                prefer_large_media=True,
                show_above_text=True,
                test=test_mode,
                rubric="news",
            )
        except SendOutcomeUnknown as e:
            log.error(
                "Обычное сообщение: исход отправки не подтверждён (%s) — дальше "
                "не пробуем, чтобы не задвоить пост", e,
            )
            uncertain = True

    if uncertain:
        if not test_mode:
            # Записываем историю в трекер как при успехе (URL/заголовок/окно/
            # отпечаток), но с пометкой uncertain — чтобы дедуп (L1/L2/L3) не
            # предложил её повторно, даже если отправка на самом деле прошла.
            # telegram_message_id неизвестен — читатели трекера (dedup.py,
            # read_history.py) обращаются к посту только по url/en_title/
            # fingerprint/posted_at, лишний ключ и None здесь не мешают.
            tracker.setdefault("windows", {})[window_key] = datetime.now(DANANG_TZ).isoformat()
            tracker.setdefault("urls", []).append(article["url"])
            tracker.setdefault("headlines", []).append(article["title"])
            tracker.setdefault("posts", []).append({
                "url": article["url"],
                "headline": article["title"],
                "en_title": article["title"],
                "posted_at": datetime.now(DANANG_TZ).isoformat(),
                "telegram_message_id": None,
                "window": window_key,
                "platform": "telegram",
                "uncertain": True,
            })
            fp = extract_fingerprint(_strip_publisher_suffix(article["title"]), article["url"])
            tracker.setdefault("fingerprints", []).append(list(fp))
            save_tracker(tracker, tracker_path)
        log.error(
            "Исход отправки не подтверждён — сообщение могло дойти или задвоиться, "
            "проверьте тред вручную. Aborting."
        )
        sys.exit(1)

    if not msg_id:
        log.error("Failed to send to Telegram. Aborting.")
        sys.exit(1)

    # Тестовый прогон не трогает трекер: иначе реальная статья пометилась бы
    # опубликованной и боевой запуск молча пропустил бы её как дубль.
    if test_mode:
        log.info("=== News bot TEST done: message_id=%s, трекер не изменён ===", msg_id)
        return

    # Update tracker
    tracker.setdefault("windows", {})[window_key] = datetime.now(DANANG_TZ).isoformat()
    tracker.setdefault("urls", []).append(article["url"])
    tracker.setdefault("headlines", []).append(article["title"])
    tracker.setdefault("posts", []).append({
        "url": article["url"],
        "headline": article["title"],
        "en_title": article["title"],
        "posted_at": datetime.now(DANANG_TZ).isoformat(),
        "telegram_message_id": msg_id,
        "window": window_key,
        "platform": "telegram",
    })
    fp = extract_fingerprint(_strip_publisher_suffix(article["title"]), article["url"])
    tracker.setdefault("fingerprints", []).append(list(fp))
    save_tracker(tracker, tracker_path)

    log.info("=== News bot done: message_id=%s, window=%s ===", msg_id, window_key)


if __name__ == "__main__":
    main()
