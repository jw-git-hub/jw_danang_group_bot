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
import re
import subprocess
import sys
import tempfile
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


# ---- Мета-комментарии модели ------------------------------------------------
# Продублировано в expat_guide_bot.py и guide_verify_apply.py: гонять единый
# импорт между этими тремя модулями смысла нет (в guide_verify_apply.py его бы
# вообще неоткуда было взять дёшево — только через этот файл, который тянет
# news_bot.py со всеми его зависимостями), а сама функция маленькая и
# самодостаточная. См. память проекта: AI-мета-комментарии не должны попадать
# в то, что видит читатель.
_META_LINE_START_RE = re.compile(
    r"^\W*(Вот|Конечно|Если нужно|Могу|Надеюсь|Here is|Sure)\b", re.MULTILINE,
)
_META_ANYWHERE_RE = re.compile(
    r"требует проверки|\(уточнить\)|для редактора|источник не найден|\bTODO\b|"
    r"```|\bJSON\b|языковая модель|as an ai",
    re.IGNORECASE,
)


def meta_violations(text: str) -> list[str]:
    """Возвращает список найденных следов AI-мета-комментариев/незавершённой правки
    в тексте (пусто — текст чист)."""
    if not text:
        return []
    violations = []
    for m in _META_LINE_START_RE.finditer(text):
        violations.append(f"преамбула в начале строки: {m.group(0)!r}")
    for m in _META_ANYWHERE_RE.finditer(text):
        violations.append(f"мета-паттерн: {m.group(0)!r}")
    return violations


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
            # exit(2), а не raise SystemExit(str) (даёт exit 1) — неизвестный
            # аргумент это ошибка использования CLI, а не сбой выполнения.
            print(f"Ошибка: неизвестный аргумент: {arg}", file=sys.stderr)
            sys.exit(2)

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
    """Вызывает `claude -p prompt`, возвращает stdout или None при ошибке.

    --strict-mcp-config --tools "" — генерация текста материала не должна иметь
    доступа ни к каким MCP-серверам или инструментам из окружения, в котором
    запущен builder; ничего позиционного после `--tools ""` быть не должно.
    cwd — временный каталог: рабочая директория проекта (с config.json и
    остальными секретами) этому вызову не нужна и не должна быть виден claude.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--strict-mcp-config", "--tools", ""],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.error("CLI `claude` не найден в PATH. Установите Claude Code CLI.")
        return None
    except subprocess.TimeoutExpired:
        log.error("Claude CLI: таймаут %d секунд", CLAUDE_TIMEOUT_SEC)
        return None

    if result.returncode != 0:
        # Диагностика (например, "usage limit reached") иногда приходит только на
        # stdout — если логировать один stderr, такая ошибка выглядит немой.
        log.error(
            "Claude CLI вернул код %d. stdout: %s stderr: %s",
            result.returncode,
            (result.stdout or "")[:500],
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
    violations = meta_violations(body)
    if violations:
        return f"обнаружены следы мета-комментариев ({violations[0]})"
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

def process_id(data: list[dict], target_id: int, preview: bool, force: bool) -> tuple[bool, bool]:
    """Обрабатывает один id. Возвращает (changed, failed):
      changed — data изменена (нужно сохранить);
      failed  — id не найден или генерация не удалась (main() должен вернуть код 1).
    Пропуск по "body уже заполнен" и preview — это НЕ failed: всё отработало штатно."""
    idx = find_entry_index(data, target_id)
    if idx is None:
        log.error("ID=%d не найден в %s", target_id, GUIDE_PATH)
        return False, True

    entry = data[idx]
    title = entry.get("title", "")
    block = entry.get("block", "misc")
    existing = entry.get("body") or ""

    if existing.strip() and not force and not preview:
        log.info("ID=%d: body уже заполнен (%d chars), пропуск (используйте --force)",
                 target_id, len(existing))
        return False, False

    body = generate_body(title, block)
    if body is None:
        log.warning("ID=%d: не удалось сгенерировать body", target_id)
        return False, True

    if preview:
        print("=" * 60)
        print(body)
        print("=" * 60)
        log.info("ID=%d: preview, не сохраняем (length=%d chars)", target_id, len(body))
        return False, False

    data[idx]["body"] = body
    if existing.strip():
        # Форсированная перегенерация меняет текст материала — старые sources/
        # verified_at подтверждали ПРЕЖНЮЮ формулировку и не обязаны быть верны для
        # новой. Правильнее считать материал непроверенным и прогнать через
        # guide_verify_apply.py заново, чем оставить читателю чужой фактчек под
        # новым текстом.
        data[idx].pop("sources", None)
        data[idx].pop("verified_at", None)
        data[idx].pop("verification_confidence", None)
        log.info("ID=%d: старые sources/verified_at/verification_confidence сброшены (перегенерация)",
                 target_id)
    log.info("ID=%d body заполнен (length=%d chars)", target_id, len(body))
    return True, False


# ---- Точка входа -----------------------------------------------------------

def main(argv: list[str]) -> int:
    ids, preview, force = parse_args(argv)
    log.info("Запуск: ids=%s, preview=%s, force=%s", ids, preview, force)

    data = load_guide()
    changed = False
    failed = False
    for target_id in ids:
        try:
            item_changed, item_failed = process_id(data, target_id, preview=preview, force=force)
        except Exception as exc:  # noqa: BLE001
            log.exception("ID=%d: непредвиденная ошибка: %s", target_id, exc)
            failed = True
            continue
        if item_changed:
            changed = True
            # Сохраняем сразу: длинный прогон по диапазону не должен терять
            # уже сгенерированные материалы из-за сбоя на середине.
            if not preview:
                save_guide_atomic(data)
                log.info("ID=%d сохранён в %s", target_id, GUIDE_PATH)
        if item_failed:
            failed = True

    if not changed:
        log.info("Изменений нет — файл не сохраняем")

    # Раньше main() всегда возвращал 0 — неудача одного id (не найден, генерация не
    # удалась после всех попыток) тонула среди ERROR/WARNING в логе, а вызывающий
    # (cron/скрипт-обёртка над диапазоном) считал прогон успешным.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
