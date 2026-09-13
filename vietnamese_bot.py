#!/usr/bin/env python3
"""
Ежедневный урок вьетнамского языка → Telegram.
Запуск: python3 vietnamese_bot.py [--dry-run] [--force]
  --dry-run — не публиковать в Telegram и не менять state (только показать пост в stdout).
  --force   — игнорировать защиту от повторной публикации в течение текущих суток (по времени Дананга).
Cron: 0 5 * * * cd /path/to/danang-bots && python3 vietnamese_bot.py >> logs/vietnamese.log 2>&1
"""

import fcntl
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_sender import send_rich_message, send_telegram_message
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


def load_state(path: Path) -> dict:
    if not path.exists():
        return _default_state()
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


def save_state(state: dict, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        # Публикация в Telegram УЖЕ прошла успешно к этому моменту — потерять state сейчас
        # значит завтра опубликовать тот же (или "заново дублирующий") урок.
        log.critical(
            "Не удалось сохранить state в %s после успешной публикации (%s)! "
            "ВРУЧНУЮ пропишите: phase=%r, current_day=%r, last_posted_at=%r, last_message_id=%r, "
            "recent_review_ids=%r — иначе при следующем запуске возможен дубль/проскок",
            path, e,
            state.get("phase"), state.get("current_day"), state.get("last_posted_at"),
            state.get("last_message_id"), state.get("recent_review_ids"),
        )


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
        last_dt = datetime.fromisoformat(last_posted_at)
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
            log.error(
                "ДЫРА в уроках: день %d отсутствует, но в файле есть более поздние дни — "
                "это не конец курса, а пропуск при генерации. current_day застрял на %d "
                "(stalled_at_day), пока дыра не будет закрыта",
                day, day,
            )
        else:
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

    excluded = set(state.get("recent_review_ids", []))
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
def main():
    log.info("=== Vietnamese bot start ===")
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram отправка отключена")

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

    state = load_state(state_path)
    validate_state(state)
    log.info("Состояние: phase=%s, current_day=%s", state.get("phase"), state.get("current_day"))

    now_danang = datetime.now(DANANG_TZ)
    if not force and not dry_run and already_posted_today(state.get("last_posted_at"), now_danang):
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

    if len(text) > TELEGRAM_MAX_LEN:
        # Потолка длины для уроков не было вообще — первый же длинный урок дал бы 400 от
        # Telegram, бот вышел бы с кодом 1, а счётчик дня не сдвинулся бы — и так каждый день.
        # Обрезать нельзя (сломает разметку/смысл), поэтому явно падаем до отправки.
        log.error(
            "Текст урока day=%s превышает лимит Telegram %d символов (получилось %d) — "
            "публикация отменена",
            lesson.get("day"), TELEGRAM_MAX_LEN, len(text),
        )
        sys.exit(1)

    if dry_run:
        log.info("DRY RUN — Telegram skipped")
        return

    thread_id = vn_cfg.get("thread_id", 1)
    # Пост уходит rich-разметкой: заголовок, абзацы, списки разбора и цитаты
    # вместо сплошного текста. Плоский вариант остаётся источником правды и
    # страховкой — если sendRichMessage не примет разметку, публикуем его,
    # а не теряем публикацию за день. Флаг --plain форсирует старый формат.
    msg_id = None
    if "--plain" not in sys.argv:
        rich_html = plain_to_rich_html(text)
        if rich_html:
            msg_id = send_rich_message(cfg, rich_html, thread_id=thread_id, rubric="vietnamese")
            if msg_id is None:
                log.warning("Rich-пост не ушёл — откатываемся на обычный текст")

    if msg_id is None:
        msg_id = send_telegram_message(cfg, text, thread_id=thread_id, rubric="vietnamese")
    if msg_id is None:
        log.error("Telegram отправка провалилась")
        sys.exit(1)

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

    save_state(state, state_path)
    log.info("Урок опубликован: day=%s, message_id=%s", lesson.get("day"), msg_id)


if __name__ == "__main__":
    main()
