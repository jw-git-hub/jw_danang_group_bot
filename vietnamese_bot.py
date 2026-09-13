#!/usr/bin/env python3
"""
Ежедневный урок вьетнамского языка → Telegram.
Запуск: python3 vietnamese_bot.py [--dry-run] [--force] [--test] [--init] [--plain]
  --dry-run — не публиковать в Telegram и не менять state (только показать пост в stdout).
  --force   — игнорировать защиту от повторной публикации в течение текущих суток (по времени Дананга).
  --test    — публиковать в telegram.test_chat_id вместо боевой группы, state не менять.
  --init    — разрешить старт с дефолтного state, если state-файл отсутствует (день 1).
Cron: 0 5 * * * cd /path/to/danang-bots && python3 vietnamese_bot.py >> logs/vietnamese.log 2>&1
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_sender import SendOutcomeUnknown, send_rich_message, send_telegram_message
from rich_render import plain_to_rich_html

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vietnamese_bot")

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
TELEGRAM_MAX_LEN = 4096
DANANG_TZ = timezone(timedelta(hours=7))
REQUIRED_LESSON_FIELDS = (
    "vietnamese", "transliteration_ru", "translation_ru", "breakdown", "context", "tone_tip",
)

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
# Lessons / state I/O
# ---------------------------------------------------------------------------
def load_lessons(path: Path) -> list:
    if not path.exists():
        log.error("Файл уроков не найден: %s", path)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            lessons = json.load(f)
    except json.JSONDecodeError as e:
        log.error("Lessons JSON error: %s", e)
        sys.exit(1)

    if not lessons:
        # random.choice([]) уронит IndexError в режиме повторения — падать нужно раньше,
        # с понятным сообщением, а не трейсбеком в середине выбора урока.
        log.error("Файл уроков %s пуст — публиковать нечего", path)
        sys.exit(1)

    return lessons


def _default_state() -> dict:
    return {
        "phase": "course",
        "current_day": 1,
        "last_posted_at": None,
        "last_message_id": None,
        "recent_review_ids": [],
    }


def _quarantine_corrupt_state(path: Path, reason) -> None:
    """State повреждён. Раньше комментарий обещал "используем дефолт без перезаписи", но
    main() после успешной отправки безусловно вызывает save_state — то есть публикуется
    урок дня 1, а state перезаписывается двойкой, и весь накопленный прогресс курса
    (потенциально 90+ дней) теряется молча. Поэтому теперь: переименовываем битый файл
    в сторону (ничего не теряем — он остаётся на диске для расследования) и останавливаемся,
    не публикуя ничего, пока прогресс не восстановят вручную."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        os.replace(path, corrupt_path)
        log.error(
            "State-файл повреждён (%s). Переименован в %s — требуется ручное восстановление, "
            "публикация отменена, чтобы не перезаписать прогресс дефолтным state",
            reason, corrupt_path,
        )
    except OSError as rename_err:
        log.error(
            "State-файл повреждён (%s), и не удалось переименовать его в .corrupt (%s) — "
            "публикация всё равно отменена",
            reason, rename_err,
        )


def load_state(path: Path, init: bool = False) -> dict:
    if not path.exists():
        # Раньше отсутствующий файл молча означал "начинаем с дня 1" — но именно так
        # выглядит и state ПОСЛЕ карантина повреждённого файла (_quarantine_corrupt_state
        # переименовывает его в сторону): run 1 корректно падал с exit 1, а run 2 на
        # следующий cron находил "пустое место" и как ни в чём не бывало публиковал
        # "День 1 / 365", будто прогресса никогда не было. Явный --init — единственный
        # способ легитимно стартовать с нуля.
        corrupt_matches = sorted(str(p) for p in path.parent.glob(f"{path.name}.corrupt-*"))
        if init:
            if corrupt_matches:
                log.warning(
                    "State-файл %s отсутствует, но найдены карантинные копии %s — "
                    "--init всё равно начинает курс с дня 1 (разберитесь с карантином вручную)",
                    path, ", ".join(corrupt_matches),
                )
            log.info("State-файл %s отсутствует, передан --init — начинаем курс с дня 1", path)
            return _default_state()
        if corrupt_matches:
            log.error(
                "нет %s: перенесите его со старого сервера или передайте --init, чтобы начать курс с дня 1 "
                "(найдены карантинные копии повреждённого state — сначала разберитесь вручную: %s)",
                path, ", ".join(corrupt_matches),
            )
        else:
            log.error(
                "нет %s: перенесите его со старого сервера или передайте --init, чтобы начать курс с дня 1",
                path,
            )
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _quarantine_corrupt_state(path, e)
        sys.exit(1)

    # dict.update() на списке/строке/числе уронит TypeError, который никто не ловит —
    # это такой же "битый" state, как невалидный JSON, просто без JSONDecodeError.
    if not isinstance(data, dict):
        log.error(
            "State-файл %s содержит не объект (%s) — слияние с дефолтом невозможно, останавливаемся",
            path, type(data).__name__,
        )
        sys.exit(1)

    # Раньше state возвращался как есть, без слияния с дефолтом — неполный файл
    # (например, без last_posted_at после ручной правки) давал KeyError на state["phase"].
    merged = _default_state()
    merged.update(data)
    return merged


def validate_state(state: dict) -> None:
    """current_day обязан быть целым >= 1 — та же проблема, что current_index в
    expat_guide_bot: None или мусор в state тихо доживает до сравнения day <= 365
    и роняет TypeError посреди рабочего запуска."""
    day = state.get("current_day")
    if not isinstance(day, int) or isinstance(day, bool) or day < 1:
        log.error(
            "State повреждён: current_day=%r невалиден (ожидалось целое число >= 1). "
            "Публикация невозможна — нужна ручная правка state-файла",
            day,
        )
        sys.exit(1)
    if state.get("phase") not in ("course", "review"):
        log.error(
            "State повреждён: phase=%r невалиден (ожидалось 'course' или 'review')",
            state.get("phase"),
        )
        sys.exit(1)
    # null трактуем как "ещё не было повторений" (см. использование ниже через `or []`),
    # а вот строка/число/dict — это уже мусор в state, который лучше поймать здесь, чем
    # получить TypeError посреди выбора урока для повторения.
    recent = state.get("recent_review_ids")
    if recent is not None and not isinstance(recent, list):
        log.error(
            "State повреждён: recent_review_ids=%r невалиден (ожидался список или null)",
            recent,
        )
        sys.exit(1)


def save_state(state: dict, path: Path) -> bool:
    """Атомарно сохраняет state. Возвращает True при успехе и False при ошибке записи —
    вызывающий сам решает, насколько это серьёзно: до отправки достаточно ERROR и отмены
    публикации, а после уже опубликованного урока это CRITICAL, потому что дубль/проскок
    дня после этого предотвратить нечем.

    flush()+fsync() перед os.replace() — без них содержимое tmp-файла может остаться в
    буфере ОС и не попасть на диск при сбое питания ровно между записью и переименованием,
    и на диске останется старый state, как будто урок не публиковался.
    """
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.error("Не удалось сохранить state в %s: %s", path, e)
        return False


# ---------------------------------------------------------------------------
# Защита от повторного запуска / конкурентных копий
# ---------------------------------------------------------------------------
def acquire_lock(lock_path: Path):
    """Неблокирующий файловый лок: вторая одновременно запущенная копия читала бы то же state
    и опубликовала бы тот же (или другой случайный) урок ещё раз. Лок держится открытым до
    конца процесса — ОС снимает его автоматически при завершении, даже при аварийном выходе."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info(
            "Другая копия vietnamese_bot уже выполняется (занят lock-файл %s) — выходим с кодом 0",
            lock_path,
        )
        sys.exit(0)
    return lock_file


def already_posted_today(last_posted_at, now_danang: datetime) -> bool:
    """last_posted_at писался в state, но никогда не читался — второй запуск в те же сутки
    (наложение cron, ручной прогон, восстановление сервера) давал одновременно дубль публикации
    И проскок урока, потому что current_day прибавлялся дважды. Сравниваем календарную дату
    последней публикации с текущей датой по времени Дананга (UTC+7)."""
    if not last_posted_at:
        return False
    try:
        # Python 3.9 не понимает суффикс "Z" в fromisoformat (это добавили только в 3.11) —
        # если state когда-нибудь придёт с сервера/системы, отдающей "...Z" вместо "+00:00"
        # (сам бот пишет только через .isoformat(), но "перенесите со старого сервера" в
        # ошибке про --init намекает, что state бывает и не отсюда), ValueError уйдёт в
        # except ниже и просто скроет реальную последнюю публикацию.
        normalized = last_posted_at[:-1] + "+00:00" if last_posted_at.endswith("Z") else last_posted_at
        last_dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    last_danang = last_dt.astimezone(DANANG_TZ)
    return last_danang.date() == now_danang.date()


# ---------------------------------------------------------------------------
# Lesson selection
# ---------------------------------------------------------------------------
def pick_lesson(lessons: list, state: dict) -> tuple[dict, bool]:
    """Возвращает (урок, is_review).

    Если урока дня current_day нет — это либо конец курса (уроков с day больше current_day
    не осталось), либо ДЫРА в данных (более поздние дни уже сгенерированы, а этот конкретный
    пропущен). Раньше оба случая тихо уходили в phase="review" с INFO-логом, а current_day
    в ветке review никогда не инкрементировался — бот навсегда застревал на одном и том же
    отсутствующем дне, и уже готовые уроки следующих дней никогда не выходили в канал, хотя
    публикации продолжали идти и рубрика снаружи выглядела здоровой.
    """
    by_day = {l.get("day"): l for l in lessons}
    day = state["current_day"]

    if state["phase"] == "course":
        if day <= 365 and day in by_day:
            return by_day[day], False

        has_later = any(isinstance(l.get("day"), int) and l.get("day") > day for l in lessons)
        if has_later:
            # ДЫРА в данных, а не конец курса — раньше это тоже уходило в phase="review" и
            # тихо публиковало случайный старый урок, а current_day не двигался НАВСЕГДА:
            # уже готовые уроки дня day+1, day+2... никогда не выходили в канал, при этом
            # публикации продолжались и рубрика снаружи выглядела здоровой. Дыра требует
            # ручного вмешательства (догенерировать урок или поправить файл) — не публикуем
            # ничего и не трогаем state, чтобы не застрять в режиме повторения молча.
            log.error(
                "ДЫРА в уроках: день %d отсутствует, но в файле есть более поздние дни — "
                "это не конец курса, а пропуск при генерации. Публикация отменена, state не "
                "изменён — закройте дыру в файле уроков и повторите запуск",
                day,
            )
            sys.exit(1)

        log.error(
            "Урок дня %d недоступен, и более поздних дней в файле нет — "
            "переключаемся в режим повторения",
            day,
        )
        state["phase"] = "review"
        state["stalled_at_day"] = day
    elif day <= 365 and day in by_day:
        # Догенерировали новые уроки — возвращаемся к курсу с того же дня
        log.info("Появился урок дня %s — возвращаемся из повторения в курс", day)
        state["phase"] = "course"
        state.pop("stalled_at_day", None)
        return by_day[day], False

    # Режим повторения: берём только уроки с day < current_day — то есть реально уже
    # опубликованные ранее. Раньше выбор шёл из ВСЕХ уроков, включая ещё не вышедшие в канал
    # (day >= current_day), и подписчик мог увидеть "🔁 ПОВТОРЯЕМ — Урок 118", которого
    # никогда не было.
    candidates = [l for l in lessons if isinstance(l.get("day"), int) and l.get("day") < day]
    if not candidates:
        log.error(
            "Режим повторения невозможен: нет ни одного ранее опубликованного урока (current_day=%d)",
            day,
        )
        sys.exit(1)

    # `or []`, а не просто .get(..., []) — ключ обычно ПРИСУТСТВУЕТ со значением None
    # (например, после ручного сброса истории повторений на null), и .get() в этом случае
    # вернёт None, а не дефолт; set(None) уронит TypeError посреди выбора урока.
    excluded = set(state.get("recent_review_ids") or [])
    filtered = [l for l in candidates if l.get("day") not in excluded]
    if not filtered:
        filtered = candidates
    return random.choice(filtered), True


def validate_lesson(lesson: dict) -> list[str]:
    """Возвращает список пустых обязательных полей (пустой список = урок готов к публикации)."""
    missing = []
    for field in REQUIRED_LESSON_FIELDS:
        value = lesson.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, str) and not value.strip():
            missing.append(field)
        elif isinstance(value, (list, dict)) and not value:
            missing.append(field)
    return missing


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _utf16_len(text: str) -> int:
    """Telegram считает длину текста в UTF-16 code units, а не в кодовых точках Python.
    len(text) занижает длину для любого символа вне BMP (эмодзи вроде 🇻🇳/🔁/📌 — суррогатная
    пара, то есть 2 unit в одной кодовой точке) — пост, полный эмодзи (а это наш формат),
    может пройти проверку len() и всё равно получить 400 от Telegram по фактической длине."""
    return len(text.encode("utf-16-le")) // 2


def format_post(lesson: dict, is_review: bool) -> str:
    day = lesson.get("day")
    vietnamese = lesson.get("vietnamese", "")
    transliteration = lesson.get("transliteration_ru", "")
    translation = lesson.get("translation_ru", "")
    breakdown = lesson.get("breakdown", []) or []
    context = lesson.get("context", "")
    tone_tip = lesson.get("tone_tip", "")
    source = (lesson.get("source") or "").lower()
    tags = lesson.get("tags", []) or []

    if is_review:
        header = f"🔁 ПОВТОРЯЕМ — Урок {day} «{vietnamese}»"
    else:
        header = f"🇻🇳 УРОК ВЬЕТНАМСКОГО — День {day} / 365"

    breakdown_lines = []
    for item in breakdown:
        word = item.get("word", "")
        tr = item.get("transliteration", "")
        meaning = item.get("meaning", "")
        breakdown_lines.append(f"• {word} ({tr}) — {meaning}")
    breakdown_block = "\n".join(breakdown_lines)

    parts = [
        header,
        "",
        f"📌 Фраза: {vietnamese}",
        f"🔊 Транскрипция: {transliteration}",
        f"🇷🇺 Перевод: {translation}",
        "",
        "📖 Разбор:",
        breakdown_block,
        "",
        "💬 Когда использовать:",
        context,
        "",
        "🎵 Тон-лайфхак:",
        tone_tip,
    ]

    if "wikibooks" in source:
        parts += ["", "📚 По материалам Wikibooks (CC BY-SA)"]

    if tags:
        parts += ["", " ".join(tags)]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ALLOWED_FLAGS = {"--dry-run", "--force", "--plain", "--test", "--init"}


def _validate_argv(argv: list) -> None:
    """Неизвестный флаг не должен доходить до лока/сети/state — раньше опечатка в cron
    (например, --dry-run с опечаткой) тихо запускала боевую публикацию вместо ожидаемого
    безопасного режима, и об этом узнавали только по факту поста в канале."""
    unknown = [a for a in argv[1:] if a not in ALLOWED_FLAGS]
    if unknown:
        print(
            f"Usage: {Path(argv[0]).name} [--dry-run] [--force] [--test] [--init] [--plain]",
            file=sys.stderr,
        )
        print(f"Неизвестный аргумент: {' '.join(unknown)}", file=sys.stderr)
        sys.exit(2)


def main():
    _validate_argv(sys.argv)

    log.info("=== Vietnamese bot start ===")
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    test_mode = "--test" in sys.argv
    init = "--init" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram отправка отключена")
    if test_mode:
        log.info("TEST mode — публикация в telegram.test_chat_id, state не меняем")

    cfg = load_config()
    base_dir = Path(__file__).parent
    vn_cfg = cfg.get("vietnamese", {}) or {}
    lessons_path = base_dir / vn_cfg.get("lessons_file", "vietnamese_lessons.json")
    state_path = base_dir / vn_cfg.get("state_file", "vietnamese_state.json")

    # Лок держим на протяжении всего запуска, чтобы вторая копия (наложение cron,
    # ручной прогон) не прочитала то же state и не опубликовала урок повторно/вразнобой.
    lock_path = state_path.with_name(state_path.name + ".lock")
    _lock_fh = acquire_lock(lock_path)  # noqa: F841 — держим ссылку, чтобы лок не снялся раньше времени

    lessons = load_lessons(lessons_path)
    log.info("Загружено уроков: %d", len(lessons))

    state = load_state(state_path, init=init)
    validate_state(state)
    log.info("Состояние: phase=%s, current_day=%s", state.get("phase"), state.get("current_day"))

    now_danang = datetime.now(DANANG_TZ)
    if not force and not dry_run and not test_mode and already_posted_today(state.get("last_posted_at"), now_danang):
        log.info(
            "Урок уже публиковался сегодня (last_posted_at=%s, дата Дананга=%s) — выходим с кодом 0, "
            "чтобы не задублировать пост и не проскочить день. Используйте --force для принудительного запуска",
            state.get("last_posted_at"), now_danang.date(),
        )
        sys.exit(0)

    lesson, is_review = pick_lesson(lessons, state)
    log.info("Выбран урок: day=%s, is_review=%s", lesson.get("day"), is_review)

    missing = validate_lesson(lesson)
    if missing:
        # Раньше format_post собирался целиком на .get(..., "") — урок с пустыми полями
        # давал валидное на вид сообщение с пустыми секциями, которое всё равно уходило в канал.
        log.error(
            "Урок дня %s не готов к публикации: пустые обязательные поля %s — отменяем отправку пустышки",
            lesson.get("day"), ", ".join(missing),
        )
        sys.exit(1)

    text = format_post(lesson, is_review)
    print("=" * 60)
    print(text)
    print("=" * 60)

    text_len = _utf16_len(text)
    if text_len > TELEGRAM_MAX_LEN:
        # Потолка длины для уроков не было вообще — первый же длинный урок дал бы 400 от
        # Telegram, бот вышел бы с кодом 1, а счётчик дня не сдвинулся бы — и так каждый день.
        # Обрезать нельзя (сломает разметку/смысл), поэтому явно падаем до отправки.
        # Длина — в UTF-16 code units (см. _utf16_len), как считает сам Telegram, а не
        # len(text): на тексте, полном эмодзи, они расходятся.
        log.error(
            "Текст урока day=%s превышает лимит Telegram %d символов (получилось %d) — "
            "публикация отменена",
            lesson.get("day"), TELEGRAM_MAX_LEN, text_len,
        )
        sys.exit(1)

    if dry_run:
        log.info("DRY RUN — Telegram skipped")
        return

    if not test_mode:
        # Проверяем возможность записи ДО отправки, пересохраняя тот же (неизменённый)
        # state: если диск только для чтения/переполнен, лучше не публиковать вовсе, чем
        # опубликовать урок и потом не суметь сдвинуть current_day (см. save_state ниже).
        if not save_state(state, state_path):
            log.error(
                "Проверка записи в state-файл %s провалилась — публикация отменена, чтобы не "
                "отправить урок без возможности сохранить прогресс",
                state_path,
            )
            sys.exit(1)

    thread_id = vn_cfg.get("thread_id", 1)
    # Пост уходит rich-разметкой: заголовок, абзацы, списки разбора и цитаты
    # вместо сплошного текста. Плоский вариант остаётся источником правды и
    # страховкой — если sendRichMessage не примет разметку, публикуем его,
    # а не теряем публикацию за день. Флаг --plain форсирует старый формат.
    msg_id = None
    if "--plain" not in sys.argv:
        rich_html = plain_to_rich_html(text)
        if rich_html:
            try:
                msg_id = send_rich_message(cfg, rich_html, thread_id=thread_id, test=test_mode, rubric="vietnamese")
            except SendOutcomeUnknown:
                # Неизвестно, ушёл ли rich-пост фактически (timeout/5xx/нечитаемый ответ) —
                # слать фоллбэк обычным текстом или ретраить нельзя: если rich всё-таки
                # доставился, получится дубль. Останавливаемся и не трогаем state, чтобы
                # снаружи разобрались вручную, что реально произошло в треде.
                log.error(
                    "Rich-отправка: исход отправки неизвестен — повтор и фоллбэк не шлём, "
                    "state не двигаем; проверьте тред вручную"
                )
                sys.exit(1)
            if msg_id is None:
                log.warning("Rich-пост не ушёл — откатываемся на обычный текст")

    if msg_id is None:
        try:
            msg_id = send_telegram_message(cfg, text, thread_id=thread_id, test=test_mode, rubric="vietnamese")
        except SendOutcomeUnknown:
            log.error(
                "Отправка: исход отправки неизвестен — повтор и фоллбэк не шлём, "
                "state не двигаем; проверьте тред вручную"
            )
            sys.exit(1)
    if msg_id is None:
        log.error("Telegram отправка провалилась")
        sys.exit(1)

    if test_mode:
        log.info("=== Vietnamese bot TEST done: message_id=%s, state не изменён ===", msg_id)
        return

    state["last_posted_at"] = datetime.now(timezone.utc).isoformat()
    state["last_message_id"] = msg_id

    if not is_review:
        state["current_day"] = state.get("current_day", 1) + 1
        if state["current_day"] > 365:
            log.info("Курс завершён после публикации — переключаемся в режим повторения")
            state["phase"] = "review"
    else:
        recent = list(state.get("recent_review_ids", []) or [])
        recent.append(lesson.get("day"))
        state["recent_review_ids"] = recent[-14:]

    if not save_state(state, state_path):
        # Публикация в Telegram УЖЕ прошла успешно к этому моменту — потерять state сейчас
        # значит завтра опубликовать тот же (или дублирующий) урок, и next run это не
        # заметит сам. exit(2) — отдельный от обычных сбоев код: мониторинг должен отличать
        # "не опубликовали" (exit 1) от "опубликовали, но состояние не сохранено" (exit 2),
        # это требует немедленного ручного вмешательства.
        log.critical(
            "Не удалось сохранить state в %s после успешной публикации! "
            "ВРУЧНУЮ пропишите: phase=%r, current_day=%r, last_posted_at=%r, last_message_id=%r, "
            "recent_review_ids=%r — иначе при следующем запуске возможен дубль/проскок",
            state_path,
            state.get("phase"), state.get("current_day"), state.get("last_posted_at"),
            state.get("last_message_id"), state.get("recent_review_ids"),
        )
        sys.exit(2)
    log.info("Урок опубликован: day=%s, message_id=%s", lesson.get("day"), msg_id)


if __name__ == "__main__":
    main()
