#!/usr/bin/env python3
"""
Еженедельный постер материалов "Гайд экспата" в Telegram-канал Дананга.
Запуск: python3 expat_guide_bot.py [--dry-run] [--force] [--plain] [--test] [--init]
  --dry-run — не публиковать в Telegram и не менять state (только показать пост в stdout).
  --force   — игнорировать защиту от повторной публикации в течение той же ISO-недели.
  --plain   — форсировать обычный (не rich) формат поста.
  --test    — публиковать в telegram.test_chat_id вместо боевой группы, state не менять.
  --init    — разрешить старт при отсутствующем state-файле (начать серию с материала 1).
Cron: 0 4 * * 0 cd /path/to/jw_danang_group_bot && python3 expat_guide_bot.py >> logs/expat_guide.log 2>&1
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_sender import send_rich_message, send_telegram_message, SendOutcomeUnknown
from rich_render import plain_to_rich_html

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("expat_guide_bot")

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
TELEGRAM_MAX_LEN = 4096
DANANG_TZ = timezone(timedelta(hours=7))

# Разрешённые флаги командной строки. Раньше неизвестный аргумент (опечатка вида
# --dry-rune) молча игнорировался и бот уходил в боевую публикацию — теперь это
# проверяется ДО любой работы с локом/сетью/state (см. _check_argv в main()).
ALLOWED_FLAGS = {"--dry-run", "--force", "--plain", "--test", "--init"}


def _check_argv(argv: list[str]) -> None:
    unknown = [a for a in argv if a not in ALLOWED_FLAGS]
    if unknown:
        print(
            "Usage: expat_guide_bot.py [--dry-run] [--force] [--plain] [--test] [--init]\n"
            f"Неизвестный аргумент: {' '.join(unknown)}",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
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
# Маппинг эмодзи блоков
# ---------------------------------------------------------------------------
BLOCK_EMOJI = {
    "visa": "🛂",
    "money": "💰",
    "transport": "🛵",
    "housing": "🏠",
    "health": "🏥",
    "food": "🛒",
    "comm": "📱",
    "misc": "📋",
}

DEFAULT_STATE: dict = {
    "current_index": 1,
    "last_posted_at": None,
    "last_message_id": None,
}


# ---------------------------------------------------------------------------
# Мета-комментарии модели
# ---------------------------------------------------------------------------
# Продублировано в expat_guide_builder.py и guide_verify_apply.py: единый импорт
# затянул бы сюда news_bot.py (и его зависимости bs4/googlenewsdecoder) только
# ради одной маленькой функции — cм. память проекта "AI-комментарии не должны
# попадать в посты", здесь это последний рубеж прямо перед отправкой в Telegram.
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


# ---------------------------------------------------------------------------
# Guide / state IO
# ---------------------------------------------------------------------------
def load_guide(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error("Гайд не найден: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Гайд JSON error: %s", e)
        sys.exit(1)


def _quarantine_corrupt_state(path: Path, reason) -> None:
    """State повреждён. Портировано из vietnamese_bot.py: раньше битый файл тихо
    подменялся дефолтом, а main() после успешной отправки безусловно вызывал
    save_state — то есть публиковался материал 1, а state перезаписывался
    дефолтом, и весь накопленный прогресс серии терялся молча (материал 1
    улетал в живую группу повторно). Поэтому теперь: переименовываем битый файл
    в сторону (ничего не теряем — он остаётся на диске для расследования) и
    останавливаемся, не публикуя ничего, пока прогресс не восстановят вручную."""
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
        if init:
            log.info("State-файл не найден, --init: начинаем серию с материала 1 (%s)", path)
            return dict(DEFAULT_STATE)
        corrupt_files = sorted(p.name for p in path.parent.glob(path.name + ".corrupt-*"))
        hint = f" Обнаружены файлы карантина: {corrupt_files}." if corrupt_files else ""
        log.error(
            "нет %s: перенесите его со старого сервера или передайте --init, чтобы начать "
            "серию с материала 1.%s",
            path, hint,
        )
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
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

    # Восстанавливаем недостающие поля
    merged = dict(DEFAULT_STATE)
    merged.update(data)
    return merged


def validate_state(state: dict) -> None:
    """current_index обязан быть целым >= 1. Если в state закрался None (или другой мусор),
    например из-за ручной правки файла, то format-строка log.info("...%d...", None) даст
    необработанный TypeError прямо в середине рабочего запуска. Лучше явно и заранее упасть
    с понятным ERROR, чем ловить трейсбек в проде."""
    idx = state.get("current_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 1:
        log.error(
            "State повреждён: current_index=%r невалиден (ожидалось целое число >= 1). "
            "Публикация невозможна — нужна ручная правка state-файла",
            idx,
        )
        sys.exit(1)


def save_state(state: dict, path: Path) -> bool:
    """Возвращает True при успешном сохранении. Вызывающий ОБЯЗАН проверить результат:
    False означает, что публикация ушла, а state — нет (см. вызов в main())."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        # Публикация в Telegram УЖЕ прошла успешно к этому моменту — потерять state сейчас
        # значит завтра/на следующей неделе опубликовать тот же материал повторно.
        # Это не самовосстанавливающаяся ошибка, поэтому CRITICAL и точные значения для ручной правки.
        log.critical(
            "Не удалось сохранить state в %s после успешной публикации (%s)! "
            "ВРУЧНУЮ пропишите: current_index=%r, last_posted_at=%r, last_message_id=%r — "
            "иначе при следующем запуске материал опубликуется повторно",
            path, e, state.get("current_index"), state.get("last_posted_at"), state.get("last_message_id"),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Защита от повторного запуска / конкурентных копий
# ---------------------------------------------------------------------------
def acquire_lock(lock_path: Path):
    """Неблокирующий файловый лок: вторая одновременно запущенная копия читала бы то же state
    и опубликовала бы тот же материал ещё раз. Лок держится открытым до конца процесса —
    ОС снимает его автоматически при завершении, даже при аварийном выходе."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info(
            "Другая копия expat_guide_bot уже выполняется (занят lock-файл %s) — выходим с кодом 0",
            lock_path,
        )
        sys.exit(0)
    return lock_file


def already_posted_this_week(last_posted_at, now_danang: datetime) -> bool:
    """Гайд публикуется раз в неделю. last_posted_at писался в state, но никогда не читался —
    из-за этого второй запуск в те же сутки (наложение cron, ручной прогон, восстановление
    сервера) публиковал дубль И одновременно продвигал счётчик на два материала вперёд.
    Сравниваем ISO-неделю последней публикации с текущей ISO-неделей по времени Дананга (UTC+7)."""
    if not last_posted_at:
        return False
    try:
        last_dt = datetime.fromisoformat(last_posted_at)
    except (ValueError, TypeError):
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    last_danang = last_dt.astimezone(DANANG_TZ)
    return last_danang.isocalendar()[:2] == now_danang.isocalendar()[:2]


# ---------------------------------------------------------------------------
# Поиск материала
# ---------------------------------------------------------------------------
def find_item(guide: list[dict], current_index: int):
    """Возвращает (status, payload):
      ("ok", item)        — материал найден и готов к публикации
      ("not_ready", item) — материал найден, но body пуст
      ("gap", next_id)    — материала с current_index нет, но в списке есть более поздние id —
                             это ДЫРА в данных (пропуск при генерации), а не конец гайда.
                             next_id — минимальный существующий id больше current_index.
      ("finished", None)  — current_index больше максимального id — гайд действительно закончен.
    Раньше find_item() не различал "дыру" и "конец гайда" и в обоих случаях возвращал None,
    из-за чего бот молча и навсегда останавливался на дыре, даже если дальше были готовые материалы.
    """
    item = next((x for x in guide if x.get("id") == current_index), None)
    if item is not None:
        body = item.get("body")
        if not body or not body.strip():
            return ("not_ready", item)
        return ("ok", item)

    max_id = max((x.get("id", 0) for x in guide), default=0)
    if current_index > max_id:
        return ("finished", None)

    later_ids = sorted(
        x.get("id") for x in guide
        if isinstance(x.get("id"), int) and x.get("id") > current_index
    )
    next_id = later_ids[0] if later_ids else None
    return ("gap", next_id)


# ---------------------------------------------------------------------------
# Форматирование поста
# ---------------------------------------------------------------------------
# Сколько источников максимум показываем под материалом: больше пяти строк
# ссылок превращают пост в библиографию и мешают читать.
MAX_SOURCES_SHOWN = 4

DISCLAIMER = (
    "⚠️ Цены, пошлины и правила во Вьетнаме меняются часто. "
    "Перед походом в учреждение сверяйтесь с источниками выше."
)


def format_sources(item: dict) -> str:
    """Блок «Проверено по источникам» — защита читателя от выдумок модели.

    Материалы гайда пишет языковая модель, поэтому каждый из них проходит
    фактчек, а подтверждающие ссылки складываются в поле sources. Показываем их
    прямо в посте: читатель может перепроверить сам, а мы не выдаём сгенерированный
    текст за истину в последней инстанции. Если источников нет — блок не рисуем
    вообще, чтобы не создавать ложного ощущения проверенности.
    """
    sources = [x for x in (item.get("sources") or [])
               if isinstance(x, dict) and x.get("url")]
    if not sources:
        return ""

    verified_at = item.get("verified_at") or ""
    when = ""
    if verified_at:
        try:
            when = " " + datetime.fromisoformat(verified_at).strftime("%d.%m.%Y")
        except ValueError:
            when = ""

    lines = [f"🔗 Проверено по источникам{when}:"]
    for src in sources[:MAX_SOURCES_SHOWN]:
        title = (src.get("title") or src["url"]).strip()
        lines.append(f"• {title} — {src['url']}")
    return "\n".join(lines) + "\n\n" + DISCLAIMER


def format_post(item: dict) -> str:
    emoji = BLOCK_EMOJI.get(item.get("block", ""), "📋")
    title = item.get("title", "")
    body = item.get("body", "")
    hashtags = " ".join(item.get("hashtags", []))

    parts = [f"{emoji} ГАЙД ЭКСПАТА — {title}", "", body]
    sources_block = format_sources(item)
    if sources_block:
        parts += ["", sources_block]
    parts += ["", hashtags]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    _check_argv(sys.argv[1:])
    log.info("=== Expat guide bot start ===")
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    test_mode = "--test" in sys.argv
    init = "--init" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram отправка отключена")
    if test_mode:
        log.info("TEST mode — постим в тестовую группу (telegram.test_chat_id), state не будет изменён")

    cfg = load_config()
    guide_cfg = cfg.get("expat_guide", {}) or {}

    if guide_cfg.get("enabled", True) is False:
        log.info("Гайд экспата выключен в config.json (expat_guide.enabled = false) — ничего не публикую")
        sys.exit(0)

    base_dir = Path(__file__).parent
    guide_path = Path(guide_cfg.get("guide_file", base_dir / "expat_guide.json"))
    state_path = Path(guide_cfg.get("state_file", base_dir / "expat_guide_state.json"))
    if not guide_path.is_absolute():
        guide_path = base_dir / guide_path
    if not state_path.is_absolute():
        state_path = base_dir / state_path

    # Лок держим на протяжении всего запуска, чтобы вторая копия (наложение cron,
    # ручной прогон) не прочитала то же state и не опубликовала тот же материал повторно.
    lock_path = state_path.with_name(state_path.name + ".lock")
    _lock_fh = acquire_lock(lock_path)  # noqa: F841 — держим ссылку, чтобы лок не снялся раньше времени

    guide = load_guide(guide_path)
    state = load_state(state_path, init=init)
    validate_state(state)
    current_index = state["current_index"]
    log.info("Текущий index=%d (всего материалов=%d)", current_index, len(guide))

    now_danang = datetime.now(DANANG_TZ)
    # test_mode публикует в тестовую группу, а не в боевую — недельная защита от дубля
    # относится только к боевому чату, поэтому тестовый прогон её не должен блокировать.
    if not force and not dry_run and not test_mode and already_posted_this_week(state.get("last_posted_at"), now_danang):
        log.info(
            "Материал уже публиковался на этой ISO-неделе (last_posted_at=%s) — выходим с кодом 0, "
            "чтобы не задублировать пост и не проскочить материал. Используйте --force для принудительного запуска",
            state.get("last_posted_at"),
        )
        sys.exit(0)

    status, payload = find_item(guide, current_index)

    if status == "finished":
        max_id = max((x.get("id", 0) for x in guide), default=0)
        # Раньше это был sys.exit(0) — "всё хорошо" для cron. На деле контент кончился,
        # и это требует реакции человека (донаполнить guide_file), поэтому теперь ERROR + exit(1).
        log.error(
            "Гайд экспата исчерпан: current_index=%d > max_id=%d — материалы закончились",
            current_index, max_id,
        )
        sys.exit(1)

    if status == "gap":
        next_id = payload
        if next_id is None:
            # Не должно происходить: раз find_item сказал "gap", next_id обязан существовать.
            log.error(
                "Дыра в данных на id=%d, но не удалось найти следующий существующий id — останавливаемся",
                current_index,
            )
            sys.exit(1)
        log.error(
            "ДЫРА в данных гайда: материала с id=%d нет в середине списка (это не конец гайда — "
            "есть более поздние id). Перескакиваем на следующий существующий id=%d и продолжаем публикацию",
            current_index, next_id,
        )
        current_index = next_id
        state["current_index"] = current_index
        status, payload = find_item(guide, current_index)
        if status not in ("ok", "not_ready"):
            log.error(
                "Непредвиденный статус %r после перескока через дыру на id=%d — останавливаемся",
                status, current_index,
            )
            sys.exit(1)

    item = payload

    if status == "not_ready":
        # Раньше это была WARNING-ветка с sys.exit(0) — код 0 неотличим от нормы для cron
        # и любого супервизора, поэтому авария (12 воскресений подряд) прошла незамеченной.
        log.error(
            "Материал id=%d title=%r ещё не подготовлен (body пуст) — публикация невозможна, "
            "выходим с кодом 1, чтобы cron/супервизор заметил проблему",
            item.get("id"), item.get("title"),
        )
        sys.exit(1)

    # status == "ok"
    text = format_post(item)
    log.info(
        "Material id=%d block=%s title=%r — пост сформирован (%d chars)",
        item.get("id"), item.get("block"), item.get("title"), len(text),
    )

    print("=" * 60)
    print(text)
    print("=" * 60)

    if len(text) > TELEGRAM_MAX_LEN:
        # Обрезать нельзя — обрежется на произвольном месте, потенциально в середине URL
        # источника или хэштегов. Лучше явно упасть и не публиковать половину поста.
        log.error(
            "Текст поста material id=%d превышает лимит Telegram %d символов (получилось %d) — "
            "публикация отменена",
            item.get("id"), TELEGRAM_MAX_LEN, len(text),
        )
        sys.exit(1)

    violations = meta_violations(text)
    if violations:
        # Последний рубеж перед отправкой: генератор и фактчек уже проверяли текст,
        # но если след AI-мета-комментария всё же дошёл досюда — лучше не публиковать
        # вообще, чем показать читателю "Вот материал..." в боевом канале.
        log.error(
            "Material id=%d: в тексте поста обнаружены следы AI-мета-комментариев (%s) — "
            "публикация отменена",
            item.get("id"), "; ".join(violations),
        )
        sys.exit(1)

    if dry_run:
        log.info("DRY RUN: пропускаем отправку, state не трогаем")
        return

    thread_id = guide_cfg.get("thread_id", 1)
    # Пост уходит rich-разметкой: заголовок, абзацы, списки разбора и цитаты
    # вместо сплошного текста. Плоский вариант остаётся источником правды и
    # страховкой — если sendRichMessage не примет разметку, публикуем его,
    # а не теряем публикацию за день. Флаг --plain форсирует старый формат.
    msg_id = None
    if "--plain" not in sys.argv:
        rich_html = plain_to_rich_html(text)
        if rich_html:
            try:
                msg_id = send_rich_message(cfg, rich_html, thread_id=thread_id, test=test_mode,
                                           rubric="expat_guide")
            except SendOutcomeUnknown:
                # Неизвестно, ушло ли сообщение (таймаут/5xx/непарсящийся ответ) — повторная
                # отправка рискует задвоить пост, а plain-фоллбэк поверх неподтверждённого
                # rich-сообщения — тем более. Останавливаемся и просим проверить тред руками.
                log.error(
                    "Material id=%d: исход отправки неизвестен — повтор и фоллбэк не шлём, "
                    "state не двигаем; проверьте тред вручную",
                    current_index,
                )
                sys.exit(1)
            if msg_id is None:
                log.warning("Rich-пост не ушёл — откатываемся на обычный текст")

    if msg_id is None:
        try:
            msg_id = send_telegram_message(cfg, text, thread_id=thread_id, test=test_mode,
                                           rubric="expat_guide")
        except SendOutcomeUnknown:
            log.error(
                "Material id=%d: исход отправки неизвестен — повтор и фоллбэк не шлём, "
                "state не двигаем; проверьте тред вручную",
                current_index,
            )
            sys.exit(1)
    if msg_id is None:
        log.error("Не удалось отправить материал id=%d в Telegram, state не обновлён", current_index)
        sys.exit(1)

    if test_mode:
        log.info("TEST: material id=%d отправлен в тестовую группу, message_id=%s, state не изменён",
                 current_index, msg_id)
        return

    state["current_index"] = current_index + 1
    state["last_posted_at"] = datetime.now(timezone.utc).isoformat()
    state["last_message_id"] = msg_id
    if not save_state(state, state_path):
        # Публикация ушла, а state — нет: следующий запуск снова прочитает старый
        # current_index и опубликует тот же материал повторно. exit(0) тут был бы
        # неотличим от нормального успеха для cron/супервизора.
        sys.exit(2)

    log.info(
        "Успешно опубликован material id=%d, message_id=%s, следующий index=%d",
        current_index, msg_id, state["current_index"],
    )


if __name__ == "__main__":
    main()
