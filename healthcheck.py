#!/usr/bin/env python3
"""Сторож автопостинга: замечает молчание рубрики и иссякающий контент.

Зачем: гайд экспата 13 воскресений подряд писал в лог «body пуст, пропускаем
неделю» и ничего не публиковал. Это выглядело как штатная ветка, наружу сигнал
не шёл, и авария всплыла только через три месяца. Сторож закрывает обе дыры:

  1. ТИШИНА — рубрика не публиковалась дольше допустимого срока.
  2. ЗАПАС — контент кончается раньше, чем его успеют догенерировать.

Источник правды о публикациях — heartbeats.json, который пишет telegram_sender
после каждой успешной отправки. Если пульса ещё нет (файл только появился),
сторож откатывается на state-файлы ботов и на логи.

Запуск:
    python3 healthcheck.py              # проверить и при проблемах послать алерт
    python3 healthcheck.py --dry-run    # только напечатать отчёт
    python3 healthcheck.py --always     # прислать отчёт даже если всё хорошо

Код возврата: 0 — всё в порядке, 1 — есть проблемы (удобно для cron MAILTO).
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_sender import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("healthcheck")

BASE = Path(__file__).parent
NOW = datetime.now(timezone.utc)

# rubric -> (человеческое имя, сколько часов молчания допустимо, лог-файл)
RUBRICS = {
    "weather":     ("Погода и дайджест", 30,  "weather.log"),
    "news":        ("Новости",           30,  "news.log"),
    "vietnamese":  ("Урок вьетнамского", 30,  "vietnamese.log"),
    "expat_guide": ("Гайд экспата",      8 * 24, "expat_guide.log"),
}

# Пороги запаса контента
LESSON_RUNWAY_WARN_DAYS = 21
GUIDE_RUNWAY_WARN_WEEKS = 4

LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        log.warning("Битый JSON %s: %s", path.name, e)
        return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_post_from_log(log_name: str) -> datetime | None:
    """Последняя удачная отправка по логу — запасной источник, если нет пульса."""
    path = BASE / "logs" / log_name
    if not path.exists():
        return None
    stamp = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "sent message_id" in line or "sent RICH message_id" in line:
                    m = LOG_TS_RE.match(line)
                    if m:
                        stamp = m.group(1)
    except OSError as e:
        log.warning("Не удалось прочитать %s: %s", path, e)
        return None
    if not stamp:
        return None
    # Логи пишутся в UTC (сервер в UTC)
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def last_post_fallback(rubric: str) -> datetime | None:
    """State-файлы ботов как второй запасной источник."""
    if rubric == "vietnamese":
        st = _read_json(BASE / "vietnamese_state.json") or {}
        return _parse_dt(st.get("last_posted_at"))
    if rubric == "expat_guide":
        st = _read_json(BASE / "expat_guide_state.json") or {}
        return _parse_dt(st.get("last_posted_at"))
    if rubric == "news":
        tr = _read_json(BASE / "danang-news-posted.json") or {}
        posts = tr.get("posts") or []
        if posts:
            return _parse_dt(posts[-1].get("posted_at"))
    return None


def check_silence(heartbeats: dict) -> list[str]:
    problems = []
    for rubric, (title, max_hours, log_name) in RUBRICS.items():
        hb = (heartbeats.get(rubric) or {}).get("last_posted_at")
        when = _parse_dt(hb) or last_post_fallback(rubric) or last_post_from_log(log_name)

        if when is None:
            problems.append(f"❓ {title}: не удалось определить дату последней публикации")
            continue

        age_h = (NOW - when).total_seconds() / 3600
        if age_h > max_hours:
            days = age_h / 24
            problems.append(
                f"🔴 {title}: молчит {days:.1f} дн. "
                f"(последняя публикация {when:%d.%m %H:%M} UTC, норма — не реже {max_hours} ч)"
            )
        else:
            log.info("OK %s: последняя публикация %s (%.1f ч назад)", title, when.strftime("%d.%m %H:%M"), age_h)
    return problems


def check_runway() -> list[str]:
    problems = []

    lessons = _read_json(BASE / "vietnamese_lessons.json") or []
    state = _read_json(BASE / "vietnamese_state.json") or {}
    if lessons and state:
        current = state.get("current_day", 1)
        available = {l.get("day") for l in lessons}
        left = len([d for d in available if isinstance(d, int) and d >= current])
        if left == 0:
            problems.append("🔴 Уроки вьетнамского: контент кончился, бот ушёл в режим повторов")
        elif left < LESSON_RUNWAY_WARN_DAYS:
            problems.append(
                f"🟡 Уроки вьетнамского: осталось {left} дн. "
                f"— пора запускать vietnamese_lesson_builder.py --month N"
            )
        else:
            log.info("OK Уроки: запас %d дней", left)

    guide = _read_json(BASE / "expat_guide.json") or []
    gstate = _read_json(BASE / "expat_guide_state.json") or {}
    if guide and gstate:
        idx = gstate.get("current_index", 1)
        ready = [x for x in guide
                 if isinstance(x.get("id"), int) and x["id"] >= idx and (x.get("body") or "").strip()]
        if not ready:
            problems.append("🔴 Гайд экспата: нет ни одного готового материала — бот молча пропустит воскресенье")
        elif len(ready) < GUIDE_RUNWAY_WARN_WEEKS:
            problems.append(
                f"🟡 Гайд экспата: осталось {len(ready)} материалов "
                f"— пора запускать expat_guide_builder.py --id N-M"
            )
        else:
            log.info("OK Гайд: запас %d материалов", len(ready))

        # Отдельно: следующий по очереди материал пуст — ровно тот случай,
        # который три месяца оставался незамеченным
        nxt = next((x for x in guide if x.get("id") == idx), None)
        if nxt is not None and not (nxt.get("body") or "").strip():
            problems.append(
                f"🔴 Гайд экспата: следующий материал id={idx} «{nxt.get('title','')[:40]}» пуст — "
                "ближайшее воскресенье будет пропущено"
            )
    return problems


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    always = "--always" in sys.argv

    log.info("=== Healthcheck start ===")
    heartbeats = _read_json(BASE / "heartbeats.json") or {}
    if not heartbeats:
        log.info("heartbeats.json пуст — используем state-файлы и логи")

    problems = check_silence(heartbeats) + check_runway()

    if problems:
        report = "🚨 АВТОПОСТИНГ — ПРОБЛЕМЫ\n\n" + "\n\n".join(problems)
    else:
        report = "✅ Автопостинг в порядке: все рубрики публикуются, запас контента достаточный"

    print("=" * 60)
    print(report)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN — алерт не отправляем")
        return 1 if problems else 0

    if problems or always:
        cfg = _read_json(BASE / "config.json")
        if not cfg:
            log.error("Нет config.json — алерт отправить не могу")
            return 1
        # Алерты идут в служебный чат, чтобы не шуметь в общине
        ops_chat = cfg["telegram"].get("ops_chat_id") or cfg["telegram"].get("test_chat_id")
        if not ops_chat:
            log.error("Не задан telegram.ops_chat_id (или test_chat_id) — алерт отправить некуда")
            return 1
        alert_cfg = {"telegram": dict(cfg["telegram"], test_chat_id=ops_chat)}
        send_telegram_message(alert_cfg, report, test=True)

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
