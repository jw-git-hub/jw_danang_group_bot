#!/usr/bin/env python3
"""Генератор тела (body) материалов гайда экспата.

Запускается ВРУЧНУЮ для заполнения поля `body` в expat_guide.json.
Использует Claude Code CLI (`claude -p`) для генерации текста.

ВАЖНО: генерация — только первый шаг. Текст пишет языковая модель, поэтому в нём
возможны выдуманные адреса, устаревшие пошлины и несуществующие учреждения.
Прежде чем материал уйдёт в канал, он обязан пройти второй шаг — фактчек с
проверкой по официальным источникам и простановкой поля sources, которое бот
показывает в посте блоком «Проверено по источникам». Ссылки при этом
проверяются запросом на живость, см. guide_verify_apply.py. Просить источники
у самой модели в этом же промпте бесполезно: выдуманный URL — ровно та
галлюцинация, от которой защищаемся.

Использование:
    python3 expat_guide_builder.py --id 1
    python3 expat_guide_builder.py --id 1 --preview
    python3 expat_guide_builder.py --id 1 --force
    python3 expat_guide_builder.py --id 1-4              # диапазон
    python3 expat_guide_builder.py --id 1-4 --preview
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Optional

from news_bot import clean_ai_output

# ---- Конфигурация ----------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
GUIDE_PATH = os.path.join(PROJECT_DIR, "expat_guide.json")

CLAUDE_TIMEOUT_SEC = 180

# Сколько раз переспросить Claude, если ответ не прошёл валидацию
MAX_ATTEMPTS = 3

MIN_BODY_LEN = 500
MAX_BODY_LEN = 3000

FORBIDDEN_MARKDOWN = ("*", "_")  # включает `**` (содержит `*`)

BLOCK_CONTEXT = {
    "visa": "визовая политика и легальное пребывание иностранцев во Вьетнаме",
    "money": "банковская система, переводы, обмен валюты, электронные платежи",
    "transport": "транспорт в Дананге и по Вьетнаму, такси, байки, общественный транспорт",
    "housing": "аренда и покупка жилья, районы Дананга, бытовые вопросы",
    "health": "медицинские услуги, страхование, аптеки",
    "food": "еда, продукты, рестораны, бытовые сервисы",
    "comm": "связь, мобильные операторы, интернет",
    "misc": "документы, регистрация, юридические вопросы",
}

PROMPT_TEMPLATE = """Ты — эксперт по жизни экспатов в Дананге, Вьетнам, и редактор Telegram-канала для русскоязычной общины.

Напиши развёрнутый материал на РУССКОМ языке по теме «{title}».

Тематический блок: {block_context}

Формат ответа:
- 3-5 абзацев, каждый начинается с тематического эмодзи
- Конкретные цифры, адреса, стоимости (актуальные на 2026 год). Если точных данных нет — пиши "уточняйте на месте", не выдумывай
- Местные нюансы Дананга: что работает там специфически, отличия от Хошимина или Ханоя
- БЕЗ markdown: никаких *, **, _, #
- Пустая строка между абзацами
- В конце — отдельный абзац начинающийся с "💡 На заметку:" с практическим советом из 1-2 предложений
- Длина: 800-1500 символов

Правила:
- Никаких преамбул («Вот материал:», «В 2026 году…»)
- Начни сразу с первого эмодзи и текста
- Никаких мета-комментариев или объяснений
- Не повторяй заголовок в теле текста
"""

# ---- Логирование -----------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("expat_guide_builder")


# ---- Парсинг аргументов ----------------------------------------------------

def parse_args(argv: list[str]) -> tuple[list[int], bool, bool]:
    """Парсит sys.argv. Возвращает (ids, preview, force).

    --id N    одиночный id
    --id N-M  диапазон включительно
    """
    ids: list[int] = []
    preview = False
    force = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--id":
            if i + 1 >= len(argv):
                raise SystemExit("Ошибка: --id требует значение")
            spec = argv[i + 1]
            ids = _parse_id_spec(spec)
            i += 2
        elif arg == "--preview":
            preview = True
            i += 1
        elif arg == "--force":
            force = True
            i += 1
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            raise SystemExit(f"Ошибка: неизвестный аргумент: {arg}")

    if not ids:
        raise SystemExit("Ошибка: укажите --id N (или --id N-M)")
    return ids, preview, force


def _parse_id_spec(spec: str) -> list[int]:
    """'1' -> [1], '1-4' -> [1,2,3,4]."""
    if "-" in spec:
        parts = spec.split("-", 1)
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            raise SystemExit(f"Ошибка: некорректный диапазон: {spec}")
        if start > end:
            raise SystemExit(f"Ошибка: диапазон {spec} — начало больше конца")
        return list(range(start, end + 1))
    try:
        return [int(spec)]
    except ValueError:
        raise SystemExit(f"Ошибка: --id ожидает число или диапазон, получено: {spec}")


# ---- Файл с гайдом ---------------------------------------------------------

def load_guide() -> list[dict]:
    with open(GUIDE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_guide_atomic(data: list[dict]) -> None:
    tmp_path = GUIDE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, GUIDE_PATH)


def find_entry_index(data: list[dict], target_id: int) -> Optional[int]:
    for i, item in enumerate(data):
        if item.get("id") == target_id:
            return i
    return None


# ---- Генерация -------------------------------------------------------------

def build_prompt(title: str, block: str) -> str:
    block_context = BLOCK_CONTEXT.get(block, block)
    return PROMPT_TEMPLATE.format(title=title, block_context=block_context)


def call_claude(prompt: str) -> Optional[str]:
    """Вызывает `claude -p prompt`, возвращает stdout или None при ошибке."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        log.error("CLI `claude` не найден в PATH. Установите Claude Code CLI.")
        return None
    except subprocess.TimeoutExpired:
        log.error("Claude CLI: таймаут %d секунд", CLAUDE_TIMEOUT_SEC)
        return None

    if result.returncode != 0:
        log.error(
            "Claude CLI вернул код %d. stderr: %s",
            result.returncode,
            (result.stderr or "")[:500],
        )
        return None

    if result.stderr:
        log.warning("Claude stderr (rc=0): %s", result.stderr[:300])

    return result.stdout.strip()


def validate_body(body: str) -> Optional[str]:
    """Возвращает текст ошибки или None если всё ок."""
    n = len(body)
    if n < MIN_BODY_LEN or n > MAX_BODY_LEN:
        return f"длина {n} вне диапазона [{MIN_BODY_LEN}; {MAX_BODY_LEN}]"
    for ch in FORBIDDEN_MARKDOWN:
        if ch in body:
            return f"найден запрещённый markdown-символ '{ch}'"
    return None


def generate_body(title: str, block: str) -> Optional[str]:
    base = build_prompt(title, block)
    log.info("Запрос к claude CLI (title=%r, block=%r, prompt=%d chars)",
             title, block, len(base))

    prompt = base
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = call_claude(prompt)
        if raw is None:
            log.warning("Попытка %d/%d: claude не ответил", attempt, MAX_ATTEMPTS)
            continue

        cleaned = clean_ai_output(raw)
        if not cleaned:
            log.warning("Попытка %d/%d: после clean_ai_output пусто", attempt, MAX_ATTEMPTS)
            prompt = base + ("\n\nВАЖНО: предыдущий ответ оказался пустым после чистки. "
                             "Верни только текст материала, без преамбул и мета-комментариев.")
            continue

        err = validate_body(cleaned)
        if err is None:
            return cleaned

        log.warning("Попытка %d/%d: валидация провалена (%s). Превью:\n%.200s",
                    attempt, MAX_ATTEMPTS, err, cleaned)
        prompt = base + (
            f"\n\nВАЖНО: предыдущий ответ отклонён — {err}. "
            f"Требования жёсткие: длина тела от {MIN_BODY_LEN} до {MAX_BODY_LEN} символов "
            f"(целься в 800-1500), символы * и _ запрещены полностью, "
            "никаких преамбул и мета-комментариев."
        )

    log.warning("%d попыток исчерпано — материал не сгенерирован", MAX_ATTEMPTS)
    return None


# ---- Обработка одного id ---------------------------------------------------

def process_id(data: list[dict], target_id: int, preview: bool, force: bool) -> bool:
    """Обрабатывает один id. Возвращает True если data изменена."""
    idx = find_entry_index(data, target_id)
    if idx is None:
        log.error("ID=%d не найден в %s", target_id, GUIDE_PATH)
        return False

    entry = data[idx]
    title = entry.get("title", "")
    block = entry.get("block", "misc")
    existing = entry.get("body") or ""

    if existing.strip() and not force and not preview:
        log.info("ID=%d: body уже заполнен (%d chars), пропуск (используйте --force)",
                 target_id, len(existing))
        return False

    body = generate_body(title, block)
    if body is None:
        log.warning("ID=%d: не удалось сгенерировать body", target_id)
        return False

    if preview:
        print("=" * 60)
        print(body)
        print("=" * 60)
        log.info("ID=%d: preview, не сохраняем (length=%d chars)", target_id, len(body))
        return False

    data[idx]["body"] = body
    log.info("ID=%d body заполнен (length=%d chars)", target_id, len(body))
    return True


# ---- Точка входа -----------------------------------------------------------

def main(argv: list[str]) -> int:
    ids, preview, force = parse_args(argv)
    log.info("Запуск: ids=%s, preview=%s, force=%s", ids, preview, force)

    data = load_guide()
    changed = False
    for target_id in ids:
        try:
            if process_id(data, target_id, preview=preview, force=force):
                changed = True
                # Сохраняем сразу: длинный прогон по диапазону не должен терять
                # уже сгенерированные материалы из-за сбоя на середине.
                if not preview:
                    save_guide_atomic(data)
                    log.info("ID=%d сохранён в %s", target_id, GUIDE_PATH)
        except Exception as exc:  # noqa: BLE001
            log.exception("ID=%d: непредвиденная ошибка: %s", target_id, exc)

    if not changed:
        log.info("Изменений нет — файл не сохраняем")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
