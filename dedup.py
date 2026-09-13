"""
Трёхуровневая дедупликация новостей.
L1: URL exact match
L2: Jaccard/Containment по английским заголовкам
L3: Entity fingerprint (работает кросс-язычно: RU↔EN)
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Сколько дней истории учитывать в L2/L3. Точный дубль по URL (L1) ловится
# без ограничения по времени, а вот "похожие" сравнения (L2/L3) с ростом
# истории только множат ложные совпадения при малой пользе — реальный
# повтор почти всегда попадает в те же 2-4 недели.
_RECENT_WINDOW_DAYS = 30

_FINGERPRINT_STOPWORDS = frozenset({
    # English stopwords
    'the', 'and', 'for', 'that', 'this', 'with', 'from', 'are', 'was',
    'were', 'been', 'have', 'has', 'had', 'will', 'would', 'could',
    'should', 'may', 'can', 'not', 'but', 'its', 'than', 'more',
    'into', 'over', 'also', 'after', 'before', 'new', 'post', 'news',
    # URL noise
    'html', 'htm', 'vnp', 'php', 'aspx', 'http', 'https', 'www', 'com',
    # Too common for Da Nang news (топонимы/служебные слова)
    'vietnam', 'viet', 'nam', 'danang', 'nang', 'dang',
    # Частые вьетнамские слоги/топонимы из URL-slug — по отдельности они
    # ничего не идентифицируют, но у двух разных статей об одном городе
    # (например, Хошимине) втроём-вчетвером дают ложное совпадение по L3
    'diem', 'nghen', 'tang', 'thao', 'toc', 'hanh', 'tai', 'tam',
    'trung', 'van', 'chi', 'minh', 'city', 'travel',
    'hanoi', 'thanh', 'hoi', 'tra',
})

# Стоп-слова для L2 (сравнение заголовков). Отдельный набор от fingerprint-
# стоп-слов: здесь нужны обычные английские служебные слова и обрывки
# доменных суффиксов издания, а не вьетнамские топонимы.
_L2_STOPWORDS = frozenset({
    'da', 'nang', 'danang', 'vietnam', 'viet', 'nam',
    'city', 'province', 'news', 'today',
    # Английские служебные слова — без них заголовки одной тематики
    # ("Da Nang ... - Vietnam News") искусственно завышают Jaccard/containment
    'the', 'and', 'for', 'to', 'in', 'of', 'a', 'with', 'new', 'on',
    'at', 'as', 'by', 'from', 'is', 'are', 'its', 'has', 'have',
    # Обрывки доменов изданий после токенизации ("vietnamnews.vn" -> vn)
    'vn', 'com',
})

# Слова/числа длиной от 2 символов, буквы и цифры — вместо наивного .split(),
# который не отсекает пунктуацию ('-', '|') и склеенные с ней токены.
_WORD_RE = re.compile(r'[A-Za-z0-9]{2,}')


def _tokenize(text):
    """Токенизация регуляркой: буквенно-цифровые последовательности от 2 символов."""
    return [m.lower() for m in _WORD_RE.findall(text or '')]


def _strip_publisher_suffix(headline):
    """RSS-заголовки почти всегда оканчиваются на ' - <издание>'
    (например ' - Tuoi Tre News | The News Gateway to Vietnam',
    ' - vietnamnews.vn', ' - DTiNews'). Этот суффикс есть у всех сохранённых
    заголовков и общий для целого издания, поэтому без обрезки два
    несвязанных заголовка одного источника дают containment под 1.0.
    Обрезаем по последнему ' - ' — само название статьи внутри себя
    так почти никогда не оформляется."""
    if not headline:
        return headline
    if ' - ' in headline:
        return headline.rsplit(' - ', 1)[0]
    return headline


def _is_recent(post, days=_RECENT_WINDOW_DAYS):
    """Попадает ли запись трекера в окно сравнения L2/L3.
    Если даты нет — считаем запись актуальной (безопасное поведение по
    умолчанию: лучше лишний раз сравнить, чем молча перестать ловить дубли)."""
    if not isinstance(post, dict):
        return True
    posted_at = post.get('posted_at')
    if not posted_at:
        return True
    try:
        dt = datetime.fromisoformat(posted_at)
    except (ValueError, TypeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(dt.tzinfo)
    return (now - dt) <= timedelta(days=days)


def _looks_like_date_fragment(num_str):
    """Число из пути URL похоже на часть даты публикации (год/месяц/день).
    Такие числа не идентифицируют статью — у двух разных статей одного
    сайта, опубликованных в один день, сразу совпадут год+месяц+день,
    что само по себе достигает порога min_overlap."""
    if len(num_str) < 3:
        return True  # месяц 1-12, день 1-31 — не длиннее 2 цифр
    if len(num_str) == 4 and 1900 <= int(num_str) <= 2100:
        return True  # похоже на год
    return False


def jaccard(a, b):
    """Word-level Jaccard similarity."""
    sa = set(_tokenize(a))
    sb = set(_tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def containment(a, b):
    """How much of a is contained in b."""
    sa = set(_tokenize(a))
    sb = set(_tokenize(b))
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


def extract_fingerprint(text, url=None):
    """Extract language-agnostic fingerprint: numbers, Latin words 3+, URL slug keywords."""
    tokens = set()
    if not text:
        if not url:
            return tokens
    else:
        # Numbers (including decimals with . or ,)
        for m in re.findall(r'\d+(?:[.,]\d+)?', text):
            tokens.add(m.replace(',', '.'))

        # Latin words 3+ chars (survive translation: NGO, MARINA, SKYTRAX, etc.)
        for m in re.findall(r'[A-Za-z]{3,}', text):
            w = m.lower()
            if w not in _FINGERPRINT_STOPWORDS:
                tokens.add(w)

    # URL path keywords
    if url:
        try:
            path = urlparse(url).path
            for w in re.findall(r'[a-z]{3,}', path.lower()):
                if w not in _FINGERPRINT_STOPWORDS:
                    tokens.add(w)
            for m in re.findall(r'\d+', path):
                # Дата публикации в пути URL — не идентификатор статьи, см. _looks_like_date_fragment
                if _looks_like_date_fragment(m):
                    continue
                tokens.add(m)
        except Exception:
            pass

    return tokens


def fingerprint_match(fp_a, fp_b, min_overlap=4, min_ratio=0.4):
    """Check if two fingerprints represent the same story."""
    if not fp_a or not fp_b:
        return False
    overlap = fp_a & fp_b
    if len(overlap) < min_overlap:
        return False
    smaller = min(len(fp_a), len(fp_b))
    if smaller == 0:
        return False
    return len(overlap) / smaller >= min_ratio


def is_duplicate(url, headline, tracker, threshold=0.35):
    """Three-layer deduplication check."""
    # L1: URL exact match — сравниваем со всей историей без ограничения по
    # времени: точный повтор URL всегда значим и стоит дёшево.
    if url in tracker.get("urls", []):
        log.info("Duplicate (L1 URL): %s", url[:80])
        return True

    posts = tracker.get("posts", [])

    # L2: Headline Jaccard/Containment (English vs English)
    # Срезаем суффикс издания и служебные слова, которые иначе завышают
    # похожесть двух несвязанных заголовков одного источника.
    cleaned_headline = ' '.join(
        w for w in _tokenize(_strip_publisher_suffix(headline))
        if w not in _L2_STOPWORDS)
    for post in posts:
        if not _is_recent(post):
            # Ограничиваем L2 недавней историей — старые совпадения почти
            # всегда ложные, а настоящий повтор ловится через L1 по URL.
            continue
        en_title = post.get("en_title") or post.get("headline", "")
        if en_title and re.search(r'[A-Za-z]{3,}', en_title):
            cleaned_en_title = ' '.join(
                w for w in _tokenize(_strip_publisher_suffix(en_title))
                if w not in _L2_STOPWORDS)
            # Skip if either cleaned headline is too short to be meaningful
            if len(cleaned_headline.split()) < 3 or len(cleaned_en_title.split()) < 3:
                continue
            j = jaccard(cleaned_headline, cleaned_en_title)
            c = containment(cleaned_headline, cleaned_en_title)
            # Порог containment-ветки ужесточён (было c>0.5 and j>0.15) —
            # при старом пороге срабатывала половина всех L2-дублей на
            # реально разных новостях с j вплоть до 0.1.
            if j > threshold or (c > 0.75 and j > 0.30):
                log.info("Duplicate (L2 headline): j=%.2f c=%.2f '%s' ~ '%s'",
                         j, c, headline[:50], en_title[:50])
                return True

    # L3: Entity fingerprint (cross-language)
    incoming_fp = extract_fingerprint(headline, url)
    if incoming_fp:
        fingerprints = tracker.get("fingerprints", [])
        # Check pre-computed fingerprints (индекс fingerprints[i] соответствует
        # posts[i] — обе структуры пополняются синхронно при постинге/импорте)
        for i, stored_fp_list in enumerate(fingerprints):
            post = posts[i] if i < len(posts) else None
            if not _is_recent(post):
                continue
            stored_fp = set(stored_fp_list)
            if fingerprint_match(incoming_fp, stored_fp):
                log.info("Duplicate (L3 fingerprint): %d shared tokens, '%s'",
                         len(incoming_fp & stored_fp), headline[:50])
                return True

        # Backward compat: compute from posts without fingerprints field
        if not fingerprints:
            for post in posts:
                if not _is_recent(post):
                    continue
                stored_fp = extract_fingerprint(
                    post.get("headline", ""), post.get("url")
                )
                if fingerprint_match(incoming_fp, stored_fp):
                    log.info("Duplicate (L3 fingerprint from post): '%s'",
                             headline[:50])
                    return True

    return False
