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

Все проверки — по входным данным, которым нельзя доверять (битые JSON,
чужеродные типы, отсутствующие файлы): падение одной проверки не должно ни
уронить сторожа целиком, ни, тем более, помешать уйти алерту об остальных
проблемах.

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
from datetime import datetime, timezone
from pathlib import Path

from telegram_sender import SendOutcomeUnknown, send_telegram_message

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
    "expat_guide": ("Гайд экспата",      8 * 24, "expat_guide.log"),  # 8 дней = 192ч
}

# Пороги запаса контента
LESSON_RUNWAY_WARN_DAYS = 21
GUIDE_RUNWAY_WARN_WEEKS = 4

LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _read_json(path: Path, expected_type: type = dict):
    """Читает JSON-файл, никогда не бросая исключение наружу.

    Отсутствие файла — тихий None (обычный случай на свежей установке).
    Любая другая проблема с файлом (битый JSON, нечитаемый — включая
    "файл" оказался директорией или недоступен по правам, невалидный UTF-8)
    логируется предупреждением и тоже возвращает None.

    expected_type проверяет форму верхнего уровня (dict для state/heartbeats/
    config, list для файлов-массивов вроде vietnamese_lessons.json) — если
    файл синтаксически валиден, но структурно не тот (например лог-трекер
    оказался списком строк вместо словаря), он всё равно расценивается как
    непригодный для чтения, а не роняет вызывающий код TypeError-ом дальше.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        log.warning("Битый или недоступный файл %s: %s", path.name, e)
        return None
    if expected_type is not None and not isinstance(data, expected_type):
        log.warning("%s: ожидался %s, получен %s — игнорируем",
                    path.name, expected_type.__name__, type(data).__name__)
        return None
    return data


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _valid_index(value) -> bool:
    """current_day/current_index обязаны быть int >= 1 — иначе (строка,
    float, отрицательное) сравнения ниже уронили бы TypeError вместо
    понятной проблемы в отчёте."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _last_post_from_one_log(path: Path) -> str | None:
    """Штамп последней НЕтестовой успешной отправки в одном лог-файле."""
    if not path.exists():
        return None
    stamp = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[TEST]" in line:
                    # Тестовые отправки (send_*(..., test=True)) в тестовую
                    # группу — не показатель того, что рубрика жива в проде.
                    continue
                if "sent message_id" in line or "sent RICH message_id" in line:
                    m = LOG_TS_RE.match(line)
                    if m:
                        stamp = m.group(1)
    except OSError as e:
        log.warning("Не удалось прочитать %s: %s", path, e)
        return None
    return stamp


def last_post_from_log(log_name: str) -> datetime | None:
    """Последняя удачная нетестовая отправка по логу — запасной источник,
    если нет пульса. Смотрим и текущий лог, и предыдущий (после logrotate) —
    иначе сразу после ротации сторож временно "слепнет" на свежий файл.
    """
    stamps = [s for s in (
        _last_post_from_one_log(BASE / "logs" / log_name),
        _last_post_from_one_log(BASE / "logs" / f"{log_name}.1"),
    ) if s]
    if not stamps:
        return None
    # Логи пишутся в UTC (сервер в UTC); из двух файлов берём более свежий штамп.
    return max(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) for s in stamps)


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
        if posts and isinstance(posts[-1], dict):
            return _parse_dt(posts[-1].get("posted_at"))
    return None


def _expat_guide_disabled() -> bool:
    """True, только если config.json.expat_guide.enabled СТРОГО равен false —
    отсутствие ключа или любое другое значение означает "рубрика включена"
    (поведение по умолчанию не меняется).

    Нарочно не использует _read_json()/log: если бы эта проверка логировала
    предупреждение о битом/отсутствующем config.json на каждом запуске —
    включая --dry-run, который раньше вообще не трогал config.json, — это
    само по себе меняло бы поведение сторожа для enabled=true/ключ отсутствует,
    а оно обязано остаться байт-в-байт прежним. Поэтому любая проблема с
    чтением здесь молча трактуется как "не выключено".
    """
    try:
        with open(BASE / "config.json", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    guide_cfg = cfg.get("expat_guide") if isinstance(cfg, dict) else None
    if not isinstance(guide_cfg, dict):
        return False
    return guide_cfg.get("enabled", True) is False


def check_silence(heartbeats: dict, guide_disabled: bool = False) -> list[str]:
    problems = []
    for rubric, (title, max_hours, log_name) in RUBRICS.items():
        if guide_disabled and rubric == "expat_guide":
            # Рубрика выключена в config.json — не читаем ни heartbeats, ни
            # state, ни лог: см. _expat_guide_disabled() и check_runway().
            continue
        try:
            hb_entry = heartbeats.get(rubric) if isinstance(heartbeats, dict) else None
            hb = hb_entry.get("last_posted_at") if isinstance(hb_entry, dict) else None
            when = _parse_dt(hb) or last_post_fallback(rubric) or last_post_from_log(log_name)

            if when is None:
                problems.append(f"❓ {title}: не удалось определить дату последней публикации")
                continue

            age_h = (NOW - when).total_seconds() / 3600
            if age_h > max_hours:
                days = age_h / 24
                when_utc = when.astimezone(timezone.utc)
                problems.append(
                    f"🔴 {title}: молчит {days:.1f} дн. "
                    f"(последняя публикация {when_utc:%d.%m %H:%M} UTC, норма — не реже {max_hours} ч)"
                )
            else:
                log.info("OK %s: последняя публикация %s (%.1f ч назад)", title, when.strftime("%d.%m %H:%M"), age_h)
        except Exception as e:  # noqa: BLE001 — одна битая рубрика не должна прятать остальные и алерт
            log.exception("Проверка тишины рубрики %s упала", rubric)
            problems.append(f"❓ Проверка «{title}» упала: {type(e).__name__}: {e}")
    return problems


def check_runway(guide_disabled: bool = False) -> list[str]:
    problems = []

    try:
        state = _read_json(BASE / "vietnamese_state.json", expected_type=dict)
        if state is None:
            problems.append("🔴 Уроки вьетнамского: vietnamese_state.json отсутствует или повреждён — бот без него не запустится")
        else:
            lessons = _read_json(BASE / "vietnamese_lessons.json", expected_type=list) or []
            current = state.get("current_day", 1)
            if not _valid_index(current):
                problems.append(f"🔴 Уроки вьетнамского: current_day в state некорректен ({current!r})")
            elif lessons:
                available = {l.get("day") for l in lessons if isinstance(l, dict)}
                phase = state.get("phase")
                stalled = state.get("stalled_at_day")
                hole_at_current = current not in available and any(
                    isinstance(d, int) and d > current for d in available
                )

                # Запас считаем только по непрерывной цепочке дней от current_day:
                # дыра сразу за текущим днём делает "формально доступные" более
                # поздние дни недостижимыми, пока бот сам через неё не перешагнёт.
                contiguous = 0
                d = current
                while d in available:
                    contiguous += 1
                    d += 1
                left = contiguous

                if phase == "review" or stalled is not None:
                    problems.append(
                        f"🔴 Уроки вьетнамского: курс не продвигается (phase={phase!r}, "
                        f"stalled_at_day={stalled!r}) — публикации застряли"
                    )
                elif hole_at_current:
                    problems.append(
                        f"🔴 Уроки вьетнамского: пропуск в контенте на текущем дне {current} — бот застрянет"
                    )
                elif left == 0:
                    problems.append("🔴 Уроки вьетнамского: контент кончился, бот ушёл в режим повторов")
                elif left < LESSON_RUNWAY_WARN_DAYS:
                    problems.append(
                        f"🟡 Уроки вьетнамского: осталось {left} дн. "
                        f"— пора запускать vietnamese_lesson_builder.py --month N"
                    )
                else:
                    log.info("OK Уроки: запас %d дней", left)
    except Exception as e:  # noqa: BLE001
        log.exception("Проверка запаса уроков вьетнамского упала")
        problems.append(f"❓ Проверка «Уроки вьетнамского» упала: {type(e).__name__}: {e}")

    if not guide_disabled:
        try:
            gstate = _read_json(BASE / "expat_guide_state.json", expected_type=dict)
            if gstate is None:
                problems.append("🔴 Гайд экспата: expat_guide_state.json отсутствует или повреждён — бот без него не запустится")
            else:
                guide = _read_json(BASE / "expat_guide.json", expected_type=list) or []
                idx = gstate.get("current_index", 1)
                if not _valid_index(idx):
                    problems.append(f"🔴 Гайд экспата: current_index в state некорректен ({idx!r})")
                elif guide:
                    ready = [x for x in guide
                             if isinstance(x, dict) and isinstance(x.get("id"), int)
                             and x["id"] >= idx and (x.get("body") or "").strip()]
                    if not ready:
                        problems.append("🔴 Гайд экспата: нет ни одного готового материала — ближайшее воскресенье будет пропущено")
                    elif len(ready) < GUIDE_RUNWAY_WARN_WEEKS:
                        problems.append(
                            f"🟡 Гайд экспата: осталось {len(ready)} материалов "
                            f"— пора запускать expat_guide_builder.py --id N-M"
                        )
                    else:
                        log.info("OK Гайд: запас %d материалов", len(ready))

                    # Отдельно: следующий по очереди материал пуст — ровно тот случай,
                    # который три месяца оставался незамеченным
                    nxt = next((x for x in guide if isinstance(x, dict) and x.get("id") == idx), None)
                    if nxt is not None and not (nxt.get("body") or "").strip():
                        problems.append(
                            f"🔴 Гайд экспата: следующий материал id={idx} «{nxt.get('title','')[:40]}» пуст — "
                            "ближайшее воскресенье будет пропущено"
                        )
        except Exception as e:  # noqa: BLE001
            log.exception("Проверка запаса гайда экспата упала")
            problems.append(f"❓ Проверка «Гайд экспата» упала: {type(e).__name__}: {e}")

    return problems


def _resolve_ops_chat(cfg: dict):
    """chat_id для алертов: telegram.ops_chat_id, если это реальное значение,
    иначе telegram.test_chat_id. Пустая строка, None и незаполненный плейсхолдер
    из config.example.json (строка вида "YOUR_...") считаются "не задано" —
    иначе алерт улетал бы в несуществующий чат и никто бы не узнал ни об этом,
    ни об исходной проблеме.
    """
    tg = cfg.get("telegram") or {}
    ops = tg.get("ops_chat_id")
    if isinstance(ops, str) and (not ops.strip() or ops.strip().startswith("YOUR_")):
        ops = None
    return ops or tg.get("test_chat_id")


ALLOWED_ARGS = {"--dry-run", "--always"}


def check_argv() -> None:
    """Неизвестный аргумент (опечатка вроде --dryrun) раньше молча
    игнорировался — из-за этого --dryrun на самом деле выполнял боевую
    отправку алерта вместо ожидаемого сухого прогона. Проверяем ДО любого
    чтения состояния или отправки, а не где-то на середине main()."""
    unknown = [a for a in sys.argv[1:] if a not in ALLOWED_ARGS]
    if unknown:
        print(
            f"Usage: {sys.argv[0]} [--dry-run] [--always]\n"
            f"Unknown argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        sys.exit(2)


def main() -> int:
    check_argv()
    dry_run = "--dry-run" in sys.argv
    always = "--always" in sys.argv

    log.info("=== Healthcheck start ===")
    heartbeats = _read_json(BASE / "heartbeats.json", expected_type=dict) or {}
    if not heartbeats:
        log.info("heartbeats.json пуст — используем state-файлы и логи")

    guide_disabled = _expat_guide_disabled()
    problems = check_silence(heartbeats, guide_disabled) + check_runway(guide_disabled)

    if problems:
        report = "🚨 АВТОПОСТИНГ — ПРОБЛЕМЫ\n\n" + "\n\n".join(problems)
    else:
        report = "✅ Автопостинг в порядке: все рубрики публикуются, запас контента достаточный"

    if guide_disabled:
        # Информационная строка: НЕ проблема, в problems не входит и на алерт/код
        # выхода не влияет (см. _expat_guide_disabled).
        report += "\n\n⏸ Гайд экспата: выключен в конфиге — не проверяется"

    print("=" * 60)
    print(report)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN — алерт не отправляем")
        return 1 if problems else 0

    if problems or always:
        cfg = _read_json(BASE / "config.json", expected_type=dict)
        if cfg is None or not isinstance(cfg.get("telegram"), dict):
            log.error("Нет config.json (или в нём нет секции telegram) — алерт отправить не могу")
            return 1
        # Алерты идут в служебный чат, чтобы не шуметь в общине
        ops_chat = _resolve_ops_chat(cfg)
        if not ops_chat:
            log.error("Не задан telegram.ops_chat_id (или test_chat_id) — алерт отправить некуда")
            return 1
        # test_thread_id относится к теме ТЕСТОВОЙ группы и не имеет отношения
        # к ops-чату (обычно это другой чат) — без сброса алерт мог случайно
        # уйти в чужую тему с тем же номером.
        alert_cfg = {"telegram": dict(cfg["telegram"], test_chat_id=ops_chat, test_thread_id=None)}
        try:
            sent = send_telegram_message(alert_cfg, report, test=True)
        except SendOutcomeUnknown as e:
            log.error("Алерт в служебный чат не удалось доставить: неопределённый исход отправки — %s", e)
            return 1
        if not sent:
            log.error("Алерт в служебный чат не удалось доставить: точный отказ отправки")
            return 1

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
