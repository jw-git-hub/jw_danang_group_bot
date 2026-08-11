#!/usr/bin/env python3
"""
Автопостинг новостей о Дананге в Telegram.
Ищет свежие новости через Google News RSS, переводит через Claude Code CLI,
отправляет в Telegram с фото. Требует установленный claude (Claude Code).

Запуск: python3 news_bot.py
Cron:
  0 9 * * *  cd /path/to/danang-bots && python3 news_bot.py >> logs/news.log 2>&1
  0 19 * * * cd /path/to/danang-bots && python3 news_bot.py >> logs/news.log 2>&1
"""

import json
import logging
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import new_decoderv1

from dedup import is_duplicate, extract_fingerprint
from facebook_poster import send_facebook_post

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
# Tracker (deduplication)
# ---------------------------------------------------------------------------
def load_tracker(cfg):
    tracker_path = Path(__file__).parent / cfg["news"]["tracker_file"]
    if tracker_path.exists():
        try:
            with open(tracker_path) as f:
                return json.load(f), tracker_path
        except json.JSONDecodeError:
            log.warning("Tracker file corrupted, starting fresh")
    return {"urls": [], "headlines": [], "posts": [], "windows": {}}, tracker_path


def save_tracker(tracker, tracker_path):
    # Keep only last 200 entries
    for key in ["urls", "headlines", "posts", "fingerprints"]:
        if key in tracker:
            tracker[key] = tracker[key][-200:]
    with open(tracker_path, "w") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
    log.info("Tracker saved: %s", tracker_path)

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

    # Freshness bonus (rough check from pub_date)
    pub = article.get("pub_date", "")
    if pub:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub)
            age_hours = (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600
            if age_hours < 12:
                score += 30
            elif age_hours < 24:
                score += 20
            elif age_hours < 48:
                score += 10
        except Exception:
            pass

    return score


def filter_articles(articles, tracker, cfg):
    """Filter out duplicates, old news, and sort by score."""
    max_age = timedelta(days=cfg["news"]["max_age_days"])
    threshold = cfg["news"]["dedup_threshold"]
    filtered = []

    for art in articles:
        # Skip duplicates
        if is_duplicate(art["url"], art["title"], tracker, threshold):
            continue

        # Skip if too old (rough check)
        pub = art.get("pub_date", "")
        if pub:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub)
                if datetime.now(dt.tzinfo) - dt > max_age:
                    log.debug("Too old: %s", art["title"][:60])
                    continue
            except Exception:
                pass

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
CLAUDE_PROMPT = """Ты — редактор новостного Telegram-канала для русскоязычных экспатов в Дананге, Вьетнам.

Напиши пост на РУССКОМ языке по этой англоязычной новости. Строго следуй формату:

[2-3 эмодзи] ЗАГОЛОВОК КАПСОМ [1-2 эмодзи]

[эмодзи] Первый абзац: суть новости в 2-3 предложениях.

[эмодзи] Второй абзац: ключевые детали, цифры, факты.

[эмодзи] Третий абзац: дополнительные подробности или контекст.

[эмодзи] Четвёртый абзац (если нужен): хронология или предыстория.

🌴 Для экспатов: как эта новость влияет на жизнь иностранцев в Дананге/Вьетнаме. 1-2 предложения.

📰 Источник: {url}

#Дананг #Danang #жизньвДананге #экспатДананг #[тема] #Vietnam2026

Правила:
- Живой русский язык, не калька с английского
- Каждый абзац начинается с тематического эмодзи
- Обязательно абзац «Для экспатов» с эмодзи 🌴
- Обязательно строка «📰 Источник: URL» — URL не менять
- Ровно 5 хэштегов: первые 4 всегда #Дананг #Danang #жизньвДананге #экспатДананг, пятый по теме
- Последний хэштег #Vietnam2026 (итого 6)
- Никакого Markdown (* ** _)
- Пустая строка между абзацами
- НЕ добавляй ссылку на Telegram-группу
- Выведи ТОЛЬКО текст поста. Начни сразу с эмодзи и заголовка. Без преамбул, комментариев, пояснений, вступлений типа «Вот пост:». Никаких мета-комментариев до или после поста
- Если текст статьи не совпадает с заголовком — пиши по заголовку и доступной информации, НЕ отказывайся и НЕ пиши пояснения

Заголовок статьи: {title}

Текст статьи:
{text}
"""


def clean_ai_output(text):
    """Remove AI meta-commentary from Claude's output, keeping only the post."""
    lines = text.split('\n')

    # Find the start: first line that begins with an emoji character
    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Check if line starts with emoji (Unicode emoji ranges)
        if stripped and _starts_with_emoji(stripped):
            start_idx = i
            break

    # Find the end: last line containing #Vietnam2026 or #Vietnam
    end_idx = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        if '#Vietnam2026' in lines[i] or '#Vietnam' in lines[i]:
            end_idx = i
            break

    # Extract the post
    post_lines = lines[start_idx:end_idx + 1]

    # Filter out any remaining meta-commentary lines within the post
    meta_patterns = [
        r'^(Примечание|Note|P\.S\.|Комментарий|Пояснение)\s*:',
        r'(я не могу|мне нужно|не соответствует|I cannot|I can\'t|предоставленный текст)',
    ]

    cleaned = []
    for line in post_lines:
        stripped = line.strip()
        is_meta = False
        for pat in meta_patterns:
            if re.search(pat, stripped, re.IGNORECASE):
                is_meta = True
                break
        if not is_meta:
            cleaned.append(line)

    return '\n'.join(cleaned).strip()


def _starts_with_emoji(text):
    """Check if text starts with an emoji character."""
    if not text:
        return False
    cp = ord(text[0])
    # Common emoji ranges
    emoji_ranges = [
        (0x1F300, 0x1FAFF),  # Misc Symbols, Emoticons, etc.
        (0x2600, 0x27BF),    # Misc symbols, Dingbats
        (0x2700, 0x27BF),    # Dingbats
        (0xFE00, 0xFE0F),    # Variation selectors
        (0x1F900, 0x1F9FF),  # Supplemental Symbols
        (0x231A, 0x23FA),    # Watch, hourglass, etc.
        (0x25AA, 0x25FE),    # Geometric shapes
        (0x2B05, 0x2B55),    # Arrows, circles
    ]
    return any(start <= cp <= end for start, end in emoji_ranges)


def generate_post(cfg, article, meta):
    """Use Claude Code CLI (claude -p) to translate and format the news post."""
    article_text = meta.get("article_text") or meta.get("og_description") or article["title"]

    prompt = CLAUDE_PROMPT.format(
        url=article["url"],
        title=article["title"],
        text=article_text,
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            log.error("Claude Code CLI error (rc=%d): %s", result.returncode, result.stderr[:500])
            return None

        post_text = result.stdout.strip()
        post_text = clean_ai_output(post_text)

        # Log stderr even on success
        if result.stderr:
            log.warning("Claude stderr (rc=0): %s", result.stderr[:500])

        log.info("Claude generated post: %d chars", len(post_text))
        log.debug("Post preview: %.200s", post_text)

        # Validate post format
        if len(post_text) < 500:
            log.error("Post too short (%d chars), likely malformed. Preview: %.200s", len(post_text), post_text)
            return None
        if "Источник:" not in post_text:
            log.error("Post missing 'Источник:' marker. Preview: %.200s", post_text)
            return None
        if "#Дананг" not in post_text:
            log.error("Post missing '#Дананг' hashtag. Preview: %.200s", post_text)
            return None

        # Safety: truncate if over 4096 (Telegram sendMessage limit)
        if len(post_text) > 4000:
            cut = post_text[:4000].rfind("\n")
            if cut > 2000:
                post_text = post_text[:cut]
            else:
                post_text = post_text[:4000]
            log.warning("Post truncated to %d chars", len(post_text))

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
def send_telegram_text(cfg, text):
    """Send text message to Telegram."""
    token = cfg["telegram"]["bot_token"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cfg["telegram"]["chat_id"],
        "message_thread_id": cfg["telegram"]["news_thread_id"],
        "text": text,
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("Telegram response not JSON: %s", resp.text[:500])
            return None
        except requests.exceptions.RequestException as e:
            log.error("Telegram request failed: %s", e)
            return None

        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            log.info("Telegram text sent: message_id=%s", msg_id)
            return msg_id

        error_code = result.get("error_code")
        if error_code == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 30)
            log.warning("Rate limited, retry %d/3 after %d seconds", attempt + 1, retry_after)
            time.sleep(retry_after + 1)
            continue

        log.error("Telegram sendMessage failed: %s", result)
        return None

    log.error("Telegram send failed after 3 retries")
    return None

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== News bot start ===")
    cfg = load_config()

    # Load tracker
    tracker, tracker_path = load_tracker(cfg)

    # Check window
    window_key = get_current_window()
    if is_window_closed(tracker, window_key):
        log.info("Window %s already closed — skipping.", window_key)
        return

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

    # Resolve Google News URLs to real article URLs
    log.info("Resolving article URLs...")
    resolved_articles = []
    resolved_urls = set()
    for art in unique_articles:
        real_url = resolve_google_news_url(art["url"])
        if real_url != art["url"]:
            log.debug("Resolved: %s -> %s", art["url"][:60], real_url[:60])
        art["url"] = real_url
        if real_url not in resolved_urls:
            resolved_urls.add(real_url)
            resolved_articles.append(art)
    unique_articles = resolved_articles
    log.info("After URL resolution: %d unique articles", len(unique_articles))

    # Filter and rank
    candidates = filter_articles(unique_articles, tracker, cfg)
    log.info("Candidates after filtering: %d", len(candidates))

    if not candidates:
        log.warning("No suitable articles found. Skipping.")
        return

    # Pick best article
    article = candidates[0]
    log.info("Selected: '%s' (score=%d)", article["title"][:80], article.get("score", 0))
    log.info("URL: %s", article["url"])

    # Extract article details
    log.info("Extracting article metadata...")
    meta = extract_article_meta(article["url"])
    log.info("OG image: %s", meta["og_image"][:100] if meta["og_image"] else "None")

    # Generate post with Claude
    log.info("Generating post with Claude...")
    post_text = generate_post(cfg, article, meta)
    if not post_text:
        log.error("Failed to generate post. Aborting.")
        sys.exit(1)

    # Send to Telegram (text only — link preview will show image from source)
    msg_id = send_telegram_text(cfg, post_text)
    send_facebook_post(cfg, post_text)

    if not msg_id:
        log.error("Failed to send to Telegram. Aborting.")
        sys.exit(1)

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
    fp = extract_fingerprint(article["title"], article["url"])
    tracker.setdefault("fingerprints", []).append(list(fp))
    save_tracker(tracker, tracker_path)

    log.info("=== News bot done: message_id=%s, window=%s ===", msg_id, window_key)


if __name__ == "__main__":
    main()
