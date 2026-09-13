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
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("guide_verify")

BASE = Path(__file__).parent
GUIDE_PATH = BASE / "expat_guide.json"
MAX_SOURCES = 4
LINK_TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DanangBot/1.0)"}


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
    # 100+ адресов занимает секунды, а не минуты.
    all_urls = {s["url"] for e in batches.values() for s in (e.get("sources") or [])
                if isinstance(s, dict) and s.get("url")}
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
            old, new = fix.get("old", ""), fix.get("new", "")
            if not old or not new:
                continue
            if old in body:
                body = body.replace(old, new, 1)
                applied_fixes += 1
                log.info("id=%d правка: «%.60s» → «%.60s»", gid, old, new)
            else:
                skipped_fixes += 1
                log.warning("id=%d правка не применена (подстрока не найдена): «%.60s»", gid, old)
        item["body"] = body

        good_sources, seen = [], set()
        for src in entry.get("sources") or []:
            if not isinstance(src, dict):
                continue
            url = (src.get("url") or "").strip()
            if not url or url in seen or not checked.get(url, (False, ""))[0]:
                continue
            seen.add(url)
            good_sources.append({"title": (src.get("title") or url)[:60], "url": url})
            if len(good_sources) >= MAX_SOURCES:
                break

        if good_sources:
            item["sources"] = good_sources
            item["verified_at"] = today
        item["verification_confidence"] = entry.get("confidence", "medium")

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
    tmp.write_text(json.dumps(guide, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, GUIDE_PATH)
    log.info("Сохранено: %s", GUIDE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
