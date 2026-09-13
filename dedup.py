"""
Трёхуровневая дедупликация новостей.
L1: URL exact match
L2: Jaccard/Containment по английским заголовкам
L3: Entity fingerprint (работает кросс-язычно: RU↔EN)
"""

import fcntl
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    # Суффикс издания в заголовке (" - Tuoi Tre News | The News Gateway to
    # Vietnam", " - vietnamnews.vn"...) обрезается _strip_publisher_suffix,
    # но старые записи трекера считались ДО того, как это стало происходить —
    # вычитаем эти слова и при сравнении (см. is_duplicate), чтобы не ловить
    # ложные совпадения только по общему изданию у двух разных статей.
    'tuoi', 'tre', 'gateway', 'vnexpress', 'international', 'dtinews',
    'vietnamnews', 'nhandan', 'vietnamplus',
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
        if not isinstance(post, dict):
            # Легаси/повреждённая запись (например, строка вместо объекта) —
            # .get() ниже уронил бы AttributeError и оставил статью неотфильтрованной.
            continue
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

    # L3: Entity fingerprint (cross-language). Заголовок чистим от суффикса
    # издания — как и при сохранении отпечатка в трекер (см. news_bot.py) —
    # иначе " - Tuoi Tre News | ..." сам по себе даёт часть совпадения.
    incoming_fp = extract_fingerprint(_strip_publisher_suffix(headline), url)
    if incoming_fp:
        fingerprints = tracker.get("fingerprints", [])
        # Check pre-computed fingerprints (индекс fingerprints[i] соответствует
        # posts[i] — обе структуры пополняются синхронно при постинге/импорте)
        for i, stored_fp_list in enumerate(fingerprints):
            post = posts[i] if i < len(posts) else None
            if not _is_recent(post):
                continue
            # Старые записи трекера считались ДО того, как суффикс издания
            # стал обрезаться и слова издания попали в стоп-лист — вычитаем
            # текущий стоп-лист из уже сохранённого отпечатка на лету, не
            # переписывая сам файл трекера.
            stored_fp = set(stored_fp_list) - _FINGERPRINT_STOPWORDS
            if fingerprint_match(incoming_fp, stored_fp):
                log.info("Duplicate (L3 fingerprint): %d shared tokens, '%s'",
                         len(incoming_fp & stored_fp), headline[:50])
                return True

        # Backward compat: compute from posts without fingerprints field
        if not fingerprints:
            for post in posts:
                if not isinstance(post, dict):
                    continue
                if not _is_recent(post):
                    continue
                stored_fp = extract_fingerprint(
                    _strip_publisher_suffix(post.get("headline", "")), post.get("url")
                )
                if fingerprint_match(incoming_fp, stored_fp):
                    log.info("Duplicate (L3 fingerprint from post): '%s'",
                             headline[:50])
                    return True

    return False


# ---------------------------------------------------------------------------
# Tracker I/O — единая реализация для news_bot.py и read_history.py.
#
# Раньше у каждого скрипта было своё load/save: news_bot.py писал не атомарно
# (усечение файла на открытии + запись поверх — сбой посреди записи оставлял
# битый файл) и на битом JSON молча "начинал с чистого листа", затирая при
# следующем сохранении всю историю дедупа; read_history.py уже делал это
# правильно (temp-файл + os.replace, отказ на битом файле вместо тихого
# сброса). Это реализация read_history, вынесенная сюда как единственная,
# плюс общий лимит хранения (было 200 у news_bot.py, слишком мало — новый
# пост вытеснял запись, которую тот же ридер тут же переимпортировал).
# ---------------------------------------------------------------------------
TRACKER_LIMIT = 1000

# Сколько дней хранить окна публикации (windows) — ключ раньше не обрезался
# никогда и рос бесконечно.
WINDOWS_RETENTION_DAYS = 14


def _posted_at_key(post):
    """Ключ сортировки по времени публикации записи.
    Если даты нет, она не разбирается или запись не dict — считаем запись
    самой свежей (безопасное поведение по умолчанию: лучше не потерять запись
    при обрезке, чем ошибочно выбросить её как "старую")."""
    posted_at = post.get("posted_at") if isinstance(post, dict) else None
    if posted_at:
        try:
            dt = datetime.fromisoformat(posted_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
    return datetime.max.replace(tzinfo=timezone.utc)


def _trim_windows(tracker, days=WINDOWS_RETENTION_DAYS):
    """windows раньше не обрезался никогда и рос вечно. Оставляем только
    окна за последние N дней (значение — ISO-timestamp последнего поста
    в это окно)."""
    windows = tracker.get("windows")
    if not isinstance(windows, dict) or not windows:
        return
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    kept = {}
    for key, value in windows.items():
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Не смогли разобрать дату — оставляем окно, чтобы не потерять
            # данные из-за неожиданного формата.
            kept[key] = value
            continue
        if dt >= cutoff:
            kept[key] = value
    tracker["windows"] = kept


def _save_corrupt_copy(path, reason):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        shutil.copy2(path, corrupt_path)
        log.error(
            "Tracker file corrupted (%s). Saved a copy to %s. Refusing to "
            "continue with an empty tracker.", reason, corrupt_path,
        )
    except OSError as copy_err:
        log.error(
            "Tracker file corrupted (%s), and failed to save a copy (%s). "
            "Refusing to continue with an empty tracker.", reason, copy_err,
        )


def load_tracker(path):
    """Загружает трекер дедупликации. path может быть str или Path.

    Битый (не-JSON или не JSON-объект верхнего уровня) файл НЕ приводит к
    тихому старту с пустого места — это стирало бы историю дедупа при
    следующем сохранении. Вместо этого сохраняем повреждённую копию рядом и
    падаем с ненулевым кодом (см. C2 про --init для намеренного пустого старта).
    """
    path = Path(path)
    if not path.exists():
        return {"urls": [], "headlines": [], "posts": [], "windows": {}}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _save_corrupt_copy(path, str(e))
        sys.exit(1)

    if not isinstance(data, dict):
        _save_corrupt_copy(path, "не JSON-объект верхнего уровня")
        sys.exit(1)

    posts = data.get("posts")
    if isinstance(posts, list) and any(not isinstance(p, dict) for p in posts):
        # Пропускаем не-dict записи posts (легаси/повреждённые), синхронно
        # выбрасывая те же позиции из парных массивов, если их длина совпадает
        # с posts — иначе фикспринты/urls/headlines разъедутся по индексу.
        n = len(posts)
        keep_idx = [i for i, p in enumerate(posts) if isinstance(p, dict)]
        skipped = n - len(keep_idx)
        data["posts"] = [posts[i] for i in keep_idx]
        for key in ("urls", "headlines", "fingerprints"):
            values = data.get(key)
            if isinstance(values, list) and len(values) == n:
                data[key] = [values[i] for i in keep_idx]
        log.warning("Пропущено %d повреждённых (не-объект) записей posts при загрузке трекера %s",
                    skipped, path)

    return data


def save_tracker(path, tracker):
    """Сохраняет трекер дедупликации. path может быть str или Path.

    - Хронологическая сортировка posts с синхронной перестановкой
      urls/headlines/fingerprints (ридер добавляет сообщения от новых к
      старым — наивная обрезка "последние N" без сортировки выбрасывала не
      самые старые записи).
    - Лимит TRACKER_LIMIT записей (см. модульный докстринг).
    - Атомарная запись: временный файл + flush + fsync + os.replace — сбой
      посреди записи (диск переполнен и т.п.) не портит существующий файл.
    """
    path = Path(path)
    posts = tracker.get("posts", [])
    if posts:
        n = len(posts)
        paired_keys = ["urls", "headlines", "fingerprints"]
        syncable = [k for k in paired_keys if len(tracker.get(k, [])) == n]
        order = sorted(range(n), key=lambda i: _posted_at_key(posts[i]))
        tracker["posts"] = [posts[i] for i in order]
        for key in syncable:
            values = tracker[key]
            tracker[key] = [values[i] for i in order]
        # Если длина списка разошлась с posts (не должно происходить в
        # норме) — синхронизировать нечем, обрезаем по-старому как запасной
        # безопасный путь, чтобы не уронить сохранение.
        for key in paired_keys:
            if key not in syncable and key in tracker:
                tracker[key] = tracker[key][-TRACKER_LIMIT:]

    for key in ["urls", "headlines", "posts", "fingerprints"]:
        if key in tracker:
            tracker[key] = tracker[key][-TRACKER_LIMIT:]

    _trim_windows(tracker)

    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    log.info("Tracker saved: %s", path)


def tracker_lock_path(tracker_path):
    """Путь лок-файла, общего для news_bot.py и read_history.py: <tracker>.lock."""
    return Path(tracker_path).with_name(Path(tracker_path).name + ".lock")


def acquire_lock(lock_path):
    """Неблокирующий файловый лок (см. expat_guide_bot.acquire_lock).

    news_bot.py и read_history.py читают/пишут один и тот же tracker_file —
    без общего лока вторая одновременно запущенная копия читала бы то же
    состояние трекера и могла бы задвоить публикацию или потерять запись при
    сохранении. Лок держится открытым до конца процесса — ОС снимает его
    автоматически при завершении, даже при аварийном выходе.
    """
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info(
            "Другой процесс (news_bot.py/read_history.py) уже выполняется "
            "(занят lock-файл %s) — выходим с кодом 0", lock_path,
        )
        sys.exit(0)
    return lock_file
