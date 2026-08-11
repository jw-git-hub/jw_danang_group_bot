"""
Трёхуровневая дедупликация новостей.
L1: URL exact match
L2: Jaccard/Containment по английским заголовкам
L3: Entity fingerprint (работает кросс-язычно: RU↔EN)
"""

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_FINGERPRINT_STOPWORDS = frozenset({
    # English stopwords
    'the', 'and', 'for', 'that', 'this', 'with', 'from', 'are', 'was',
    'were', 'been', 'have', 'has', 'had', 'will', 'would', 'could',
    'should', 'may', 'can', 'not', 'but', 'its', 'than', 'more',
    'into', 'over', 'also', 'after', 'before', 'new', 'post', 'news',
    # URL noise
    'html', 'htm', 'vnp', 'php', 'aspx', 'http', 'https', 'www', 'com',
    # Too common for Da Nang news
    'vietnam', 'viet', 'nam', 'danang', 'nang', 'dang',
})


def jaccard(a, b):
    """Word-level Jaccard similarity."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def containment(a, b):
    """How much of a is contained in b."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
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
                tokens.add(m)
        except Exception:
            pass

    return tokens


def fingerprint_match(fp_a, fp_b, min_overlap=3, min_ratio=0.4):
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
    # L1: URL exact match
    if url in tracker.get("urls", []):
        log.info("Duplicate (L1 URL): %s", url[:80])
        return True

    # L2: Headline Jaccard/Containment (English vs English)
    # Strip common words that inflate similarity for Da Nang news
    _l2_stopwords = {'da', 'nang', 'danang', 'vietnam', 'viet', 'nam',
                     'city', 'province', 'news', 'today'}
    cleaned_headline = ' '.join(
        w for w in headline.lower().split() if w not in _l2_stopwords)
    for post in tracker.get("posts", []):
        en_title = post.get("en_title") or post.get("headline", "")
        if en_title and re.search(r'[A-Za-z]{3,}', en_title):
            cleaned_en_title = ' '.join(
                w for w in en_title.lower().split() if w not in _l2_stopwords)
            # Skip if either cleaned headline is too short to be meaningful
            if len(cleaned_headline.split()) < 3 or len(cleaned_en_title.split()) < 3:
                continue
            j = jaccard(cleaned_headline, cleaned_en_title)
            c = containment(cleaned_headline, cleaned_en_title)
            if j > threshold or (c > 0.5 and j > 0.15):
                log.info("Duplicate (L2 headline): j=%.2f c=%.2f '%s' ~ '%s'",
                         j, c, headline[:50], en_title[:50])
                return True

    # L3: Entity fingerprint (cross-language)
    incoming_fp = extract_fingerprint(headline, url)
    if incoming_fp:
        # Check pre-computed fingerprints
        for stored_fp_list in tracker.get("fingerprints", []):
            stored_fp = set(stored_fp_list)
            if fingerprint_match(incoming_fp, stored_fp):
                log.info("Duplicate (L3 fingerprint): %d shared tokens, '%s'",
                         len(incoming_fp & stored_fp), headline[:50])
                return True

        # Backward compat: compute from posts without fingerprints field
        if not tracker.get("fingerprints"):
            for post in tracker.get("posts", []):
                stored_fp = extract_fingerprint(
                    post.get("headline", ""), post.get("url")
                )
                if fingerprint_match(incoming_fp, stored_fp):
                    log.info("Duplicate (L3 fingerprint from post): '%s'",
                             headline[:50])
                    return True

    return False
