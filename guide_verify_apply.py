#!/usr/bin/env python3
"""Применение результатов фактчека к expat_guide.json.

Материалы гайда пишет языковая модель, поэтому каждый проходит проверку:
отдельные агенты сверяют утверждения с официальными источниками и складывают
вердикты в JSON-файлы. Этот скрипт переносит их результат в гайд:

  1. применяет уверенные исправления фактов (только точные совпадения подстрок);
  2. прикрепляет источники, ПРЕДВАРИТЕЛЬНО проверив каждую ссылку запросом —
     выдуманный URL это ровно та галлюцинация, от которой мы защищаемся,
     поэтому непроверенная ссылка в гайд не попадает;
  3. проставляет дату проверки, которую бот показывает в посте.

Запуск:
    python3 guide_verify_apply.py --dir <каталог с batch_*.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("guide_verify")

BASE = Path(__file__).parent


def _load_guide_path() -> Path:
    """GUIDE_PATH берём из config.json (expat_guide.guide_file), если он есть и читается —
    так verify_apply, builder и бот целятся в один и тот же файл при нестандартном
    расположении. Конфига может не быть (например, в тестовом окружении) — тогда
    молча используем дефолт рядом со скриптом, как и раньше."""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        guide_file = (cfg.get("expat_guide") or {}).get("guide_file")
    except (OSError, json.JSONDecodeError):
        guide_file = None
    if not guide_file:
        return BASE / "expat_guide.json"
    p = Path(guide_file)
    return p if p.is_absolute() else BASE / p


GUIDE_PATH = _load_guide_path()
MAX_SOURCES = 4
LINK_TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DanangBot/1.0)"}

# ---- Мета-комментарии модели ------------------------------------------------
# Продублировано из expat_guide_builder.py (см. комментарий там): импортировать
# builder отсюда значило бы тянуть news_bot.py и его зависимости (bs4,
# googlenewsdecoder) в скрипт фактчека, которому они не нужны.
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


def check_url(url: str) -> tuple[str, bool, str]:
    """Проверяет, что ссылка жива. Возвращает (url, ok, причина)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=LINK_TIMEOUT,
                            allow_redirects=True, stream=True)
        resp.close()
    except requests.exceptions.RequestException as e:
        return url, False, type(e).__name__
    if resp.status_code >= 400:
        return url, False, f"HTTP {resp.status_code}"
    return url, True, f"HTTP {resp.status_code}"


def load_batches(dir_path: Path) -> dict[int, dict]:
    results: dict[int, dict] = {}
    files = sorted(dir_path.glob("batch_*.json"))
    if not files:
        log.error("В %s нет файлов batch_*.json", dir_path)
        sys.exit(1)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("%s: битый JSON (%s) — пропускаем файл", f.name, e)
            continue
        if not isinstance(data, list):
            log.error("%s: ожидался массив — пропускаем", f.name)
            continue
        for entry in data:
            if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                results[entry["id"]] = entry
        log.info("%s: записей %d", f.name, len(data))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="каталог с batch_*.json")
    ap.add_argument("--dry-run", action="store_true", help="только отчёт, файл не менять")
    args = ap.parse_args()

    batches = load_batches(Path(args.dir))
    guide = json.loads(GUIDE_PATH.read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in guide}

    # Сначала собираем все ссылки и проверяем их параллельно — так проверка
    # 100+ адресов занимает секунды, а не минуты. Обрезаем пробелы ЗДЕСЬ же:
    # ниже good_sources ищет результат проверки по такому же обрезанному url —
    # если тут оставить сырой (с пробелами) вариант, ключи разойдутся и живая
    # ссылка с случайным пробелом в batch-файле всегда будет выглядеть битой.
    all_urls = {(s.get("url") or "").strip() for e in batches.values() for s in (e.get("sources") or [])
                if isinstance(s, dict) and (s.get("url") or "").strip()}
    log.info("Проверяю живость %d уникальных ссылок...", len(all_urls))
    with ThreadPoolExecutor(max_workers=10) as pool:
        checked = {url: (ok, why) for url, ok, why in pool.map(check_url, all_urls)}
    dead = [u for u, (ok, _) in checked.items() if not ok]
    log.info("Живых: %d, битых: %d", len(all_urls) - len(dead), len(dead))
    for u in dead:
        log.warning("  битая ссылка отброшена: %s (%s)", u[:90], checked[u][1])

    applied_fixes = skipped_fixes = 0
    today = date.today().isoformat()
    report = []

    for gid, entry in sorted(batches.items()):
        item = by_id.get(gid)
        if item is None:
            log.warning("id=%d нет в гайде — пропускаем", gid)
            continue

        body = item.get("body") or ""
        for fix in entry.get("corrections") or []:
            if not isinstance(fix, dict):
                skipped_fixes += 1
                log.warning("id=%d правка пропущена: элемент corrections не объект (%r)", gid, fix)
                continue
            old, new = fix.get("old", ""), fix.get("new", "")
            if not old or not new:
                continue
            violations = meta_violations(new)
            if violations:
                skipped_fixes += 1
                log.warning("id=%d правка пропущена (следы мета-комментариев: %s): «%.60s»",
                            gid, "; ".join(violations), new)
                continue
            if old not in body:
                skipped_fixes += 1
                log.warning("id=%d правка не применена (подстрока не найдена): «%.60s»", gid, old)
                continue
            if old in new and new in body:
                # Идемпотентность: batch может применяться повторно (повторный фактчек,
                # восстановление после сбоя). old — подстрока new, поэтому "old in body"
                # осталась бы истинной и после первого применения — без этой проверки
                # правка задваивалась бы на каждом повторном запуске.
                log.info("id=%d правка уже применена ранее, пропуск: «%.60s»", gid, old)
                continue
            body = body.replace(old, new, 1)
            applied_fixes += 1
            log.info("id=%d правка: «%.60s» → «%.60s»", gid, old, new)
        item["body"] = body

        good_sources, seen = [], set()
        for src in entry.get("sources") or []:
            if not isinstance(src, dict):
                continue
            url = (src.get("url") or "").strip()
            if not url or url in seen or not checked.get(url, (False, ""))[0]:
                continue
            title = (src.get("title") or url)[:60]
            violations = meta_violations(title)
            if violations:
                log.warning("id=%d источник пропущен (следы мета-комментариев в title: %s): %s",
                            gid, "; ".join(violations), url)
                continue
            seen.add(url)
            good_sources.append({"title": title, "url": url})
            if len(good_sources) >= MAX_SOURCES:
                break

        confidence = entry.get("confidence")
        if good_sources:
            item["sources"] = good_sources
            item["verified_at"] = today
            # Дефолт "medium" при отсутствующем/пустом confidence был бы придуманной
            # оценкой — batch её не давал. Пишем поле, только если батч сам её прислал.
            if confidence:
                item["verification_confidence"] = confidence
            else:
                item.pop("verification_confidence", None)
        else:
            # Повторный фактчек не нашёл ни одного живого источника — прежние
            # sources/verified_at/verification_confidence относятся к состоянию,
            # которое уже не подтверждено, и держать их дальше значит выдавать
            # устаревшую проверку за действующую.
            item.pop("sources", None)
            item.pop("verified_at", None)
            item.pop("verification_confidence", None)

        report.append((gid, item["title"][:40], len(good_sources),
                       entry.get("confidence", "?"), len(entry.get("verdicts") or [])))

    print("\n" + "=" * 78)
    print(f"{'id':>3} | {'материал':40} | ссылок | доверие | проверок")
    print("-" * 78)
    for gid, title, n_src, conf, n_ver in report:
        print(f"{gid:>3} | {title:40} | {n_src:^6} | {conf:^7} | {n_ver:^8}")
    print("=" * 78)
    print(f"Правок применено: {applied_fixes}, не применено: {skipped_fixes}")
    without = [x["id"] for x in guide if not x.get("sources")]
    print(f"Материалов без источников: {len(without)} {without if without else ''}")

    if args.dry_run:
        log.info("DRY RUN — expat_guide.json не изменён")
        return 0

    tmp = GUIDE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(guide, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, GUIDE_PATH)
    log.info("Сохранено: %s", GUIDE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
