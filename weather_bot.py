#!/usr/bin/env python3
"""
Ежедневный прогноз погоды и AQI для Дананга → Telegram.
Запуск: python3 weather_bot.py [--dry-run] [--test] [--force] [--plain] [--rich-list]
Cron: 0 0 * * * cd /path/to/danang-bots && python3 weather_bot.py >> logs/weather.log 2>&1
  (00:00 UTC = 07:00 в Дананге — сервер работает в UTC)
"""

import fcntl
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from facebook_poster import send_facebook_post
from telegram_sender import HEARTBEAT_PATH, SendOutcomeUnknown, send_rich_message, send_telegram_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weather_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"
LOCK_PATH = Path(__file__).parent / "weather_bot.lock"

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
# Weather data (Open-Meteo — free, no key)
# ---------------------------------------------------------------------------
def fetch_weather(cfg):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": cfg["weather"]["latitude"],
        "longitude": cfg["weather"]["longitude"],
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": cfg["weather"]["timezone"],
        "forecast_days": 4,
    }
    backoff = [5, 15, 30]
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                log.warning("Weather fetch attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, backoff[attempt])
                time.sleep(backoff[attempt])
            else:
                log.error("Weather fetch attempt 3/3 failed: %s — giving up", e)
                raise

# ---------------------------------------------------------------------------
# AQI data (AQICN)
# ---------------------------------------------------------------------------
def fetch_aqi(cfg):
    token = cfg["weather"]["aqi_token"]
    url = f"https://api.waqi.info/feed/danang/?token={token}"
    backoff = [5, 15, 30]
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                log.warning("AQI API returned status: %s", data.get("status"))
                return None
            aqi_payload = data["data"]
            # AQICN штатно отдаёт "aqi": "-" (строка), когда у станции нет
            # свежих измерений — при этом status всё ещё "ok". Приводим к int
            # заранее, чтобы дальше по коду (aqi_level, форматирование) везде
            # был либо чистый int, либо явный None — без строк-сюрпризов.
            try:
                aqi_payload["aqi"] = int(aqi_payload.get("aqi"))
            except (KeyError, TypeError, ValueError):
                log.warning(
                    "AQI: станция вернула нечисловое aqi=%r (нет свежих данных) — публикуем без AQI",
                    aqi_payload.get("aqi") if isinstance(aqi_payload, dict) else aqi_payload,
                )
                aqi_payload["aqi"] = None
            return aqi_payload
        except requests.exceptions.RequestException as e:
            # Токен лежит в query string — str(e) у requests (таймауты, DNS,
            # HTTPError от raise_for_status) содержит полный URL вместе с ним;
            # без маскирования токен утекает в logs/*.log при любом сетевом сбое.
            masked = str(e).replace(token, "<AQI_TOKEN>")
            if attempt < 2:
                log.warning("AQI fetch attempt %d/3 failed: %s — retrying in %ds", attempt + 1, masked, backoff[attempt])
                time.sleep(backoff[attempt])
            else:
                log.error("AQI fetch attempt 3/3 failed: %s — giving up", masked)
                # Новое исключение с уже замаскированным текстом и без цепочки
                # (from None) — иначе main(), поймав его как `except Exception`,
                # залогирует %s от ЭТОГО объекта (что ок), но traceback/repr
                # исходного e с сырым токеном не должен всплыть ни при каких
                # условиях логирования выше по стеку.
                raise RuntimeError(masked) from None

# ---------------------------------------------------------------------------
# Курсы валют (open.er-api.com — бесплатно, без ключа; frankfurter — fallback)
# ---------------------------------------------------------------------------
# Разумный диапазон курса USD/VND. Если апстрим отдаст мусор (например VND=1
# при сломанном API), результат будет truthy и пролезет в пост как «1 USD ≈
# 1 VND» — поэтому явно отбрасываем всё, что выходит за пределы правдоподобия.
USD_VND_SANE_RANGE = (20_000, 35_000)


def _usd_vnd_is_sane(usd_to_vnd):
    return USD_VND_SANE_RANGE[0] <= usd_to_vnd <= USD_VND_SANE_RANGE[1]


def fetch_exchange_rates():
    """Возвращает {'usd_to_vnd': float, 'rub_to_vnd': float} или None при провале.

    Primary:   https://open.er-api.com/v6/latest/USD  (формат {"rates": {...}})
    Fallback:  https://api.frankfurter.app/latest  (база EUR — конвертируем).
    """
    # Попытка 1: open.er-api.com (база USD напрямую)
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates") or {}
        vnd = rates.get("VND")
        rub = rates.get("RUB")
        if vnd and rub:
            usd_to_vnd = float(vnd)
            if _usd_vnd_is_sane(usd_to_vnd):
                return {
                    "usd_to_vnd": usd_to_vnd,
                    "rub_to_vnd": usd_to_vnd / float(rub),
                }
            log.warning("FX primary: usd_to_vnd=%.2f вне разумного диапазона %s — отбрасываем", usd_to_vnd, USD_VND_SANE_RANGE)
        else:
            log.warning("FX primary: отсутствуют VND/RUB в ответе: %s", list(rates)[:10])
    except requests.exceptions.RequestException as e:
        log.warning("FX primary fetch failed: %s", e)
    except (ValueError, KeyError, TypeError) as e:
        log.warning("FX primary parse failed: %s", e)

    # Попытка 2: frankfurter (база EUR, без указания base — иначе 404)
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"symbols": "VND,RUB,USD"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates") or {}
        eur_to_vnd = rates.get("VND")
        eur_to_rub = rates.get("RUB")
        eur_to_usd = rates.get("USD")
        if eur_to_vnd and eur_to_rub and eur_to_usd:
            usd_to_vnd = float(eur_to_vnd) / float(eur_to_usd)
            usd_to_rub = float(eur_to_rub) / float(eur_to_usd)
            if _usd_vnd_is_sane(usd_to_vnd):
                return {
                    "usd_to_vnd": usd_to_vnd,
                    "rub_to_vnd": usd_to_vnd / usd_to_rub,
                }
            log.warning("FX fallback: usd_to_vnd=%.2f вне разумного диапазона %s — отбрасываем", usd_to_vnd, USD_VND_SANE_RANGE)
        else:
            log.warning("FX fallback: отсутствуют курсы в ответе frankfurter: %s", list(rates)[:10])
    except requests.exceptions.RequestException as e:
        log.warning("FX fallback fetch failed: %s", e)
    except (ValueError, KeyError, TypeError) as e:
        log.warning("FX fallback parse failed: %s", e)

    return None

# ---------------------------------------------------------------------------
# Персистентный кэш цен (бензин/золото)
# ---------------------------------------------------------------------------
# Раньше при провале всех источников fetch_petrol_price/fetch_gold_price
# возвращали захардкоженные константы, которые молча публиковались как
# «сегодняшняя» цена месяцами (68 варнингов в логах, которые никто не читал).
# Теперь при успешном скрейпе кладём значение в JSON-кэш рядом с проектом,
# а при провале — берём последнее известное значение ИЗ КЭША и возвращаем
# его вместе с датой получения, чтобы в посте было видно возраст цифры.
PRICE_CACHE_PATH = Path(__file__).parent / "price_cache.json"
PRICE_CACHE_MAX_AGE_DAYS = 7

# Диапазоны для валидации значения ИЗ КЭША — те же, что живой парсер уже
# применяет к свежесобранной цене (см. fetch_petrol_price/fetch_gold_price).
# Кэш — это файл на диске, который мог быть обрезан аварийным завершением
# процесса или записан старой версией кода; без этой проверки null/строка/
# число-мусор из кэша доходили до format_vnd() и роняли оба форматтера поста.
_CACHE_SANE_RANGES = {
    "petrol": (15_000, 40_000),
    "gold": (5_000_000, 25_000_000),
}

# Устанавливается в main() из --dry-run. В dry-run мы всё равно ходим за
# ценами (чтобы показать честный превью поста), но не должны трогать
# price_cache.json на диске — это режим "только посмотреть".
_DRY_RUN = False


def _load_price_cache():
    try:
        with open(PRICE_CACHE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Price cache: не удалось прочитать %s: %s", PRICE_CACHE_PATH, e)
        return {}


def _save_price_cache(cache):
    # Атомарная запись через temp-файл в той же директории + os.replace.
    # Раньше имя temp-файла было фиксированным (.json.tmp) — два процесса,
    # пишущих кэш одновременно (до появления файлового лока или в обход
    # него), затирали временный файл друг друга. mkstemp даёт уникальное
    # имя, так что параллельные записи больше не сталкиваются.
    fd, tmp_name = tempfile.mkstemp(
        prefix=PRICE_CACHE_PATH.name + ".", suffix=".tmp", dir=str(PRICE_CACHE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, PRICE_CACHE_PATH)
    except OSError as e:
        log.warning("Price cache: не удалось сохранить %s: %s", PRICE_CACHE_PATH, e)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _update_price_cache(key, value, label=None):
    if _DRY_RUN:
        return
    cache = _load_price_cache()
    entry = {
        "value": value,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if label is not None:
        entry["label"] = label
    cache[key] = entry
    _save_price_cache(cache)


def _get_cached_price(key, max_age_days=PRICE_CACHE_MAX_AGE_DAYS):
    """Возвращает (value, fetched_at, label) из кэша, если он не старше
    max_age_days (offset-aware datetime, UTC) и value прошло проверку
    типа/диапазона, иначе None — старый или битый кэш лучше не публиковать
    вовсе, чем выдавать за актуальную цену многомесячной давности (или уронить
    форматирование поста).

    label — то, что было сохранено вместе со значением (см.
    _update_price_cache), либо None для записей старого формата без label;
    вызывающий код сам подставляет дефолтную подпись в этом случае.
    """
    entry = _load_price_cache().get(key)
    if not entry:
        return None
    try:
        value = entry["value"]
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    # bool — подкласс int в Python, поэтому исключаем его отдельно; null/строка
    # ("21.830" и т.п.) в кэше раньше долетали до format_vnd() и роняли пост.
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    sane_range = _CACHE_SANE_RANGES.get(key)
    if sane_range and not (sane_range[0] <= value <= sane_range[1]):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched_at > timedelta(days=max_age_days):
        return None
    return value, fetched_at, entry.get("label")

# ---------------------------------------------------------------------------
# Цена бензина RON 95-III (webgia.com — агрегатор Petrolimex)
# ---------------------------------------------------------------------------
def fetch_petrol_price():
    """Возвращает (value: int, fetched_at: datetime|None, label: str) — цену бензина
    в донгах за литр, либо None, если нет ни свежих данных, ни валидного кэша.

    fetched_at is None, когда цифра получена прямо сейчас; иначе это момент
    последнего успешного сбора — пост честно показывает возраст цены, а не выдаёт
    старую за сегодняшнюю.

    Про label. Вьетнам перевёл рынок на смесь E10, и прежний «Xăng RON 95-III»
    в таблицах остался строкой с прочерками — именно поэтому парсер, жёстко
    привязанный к этому названию, месяцами возвращал пустоту и подменялся
    константой. Поэтому ищем ЛЮБУЮ строку с RON 95 (включая E10 RON 95-III),
    а если её нет — берём E5 RON 92-II, который на бирже котируется всегда, и
    честно подписываем в посте, какой именно бензин показан.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    }

    # Приоритет марок: сперва 95-я в любом исполнении, затем E5 RON 92 как замена
    GRADES = (
        (("e10 ron 95", "ron 95-iii", "ron 95 iii"), "E10 RON 95"),
        (("e5 ron 92", "ron 92-ii", "ron 92 ii"), "E5 RON 92"),
    )

    try:
        resp = requests.get("https://webgia.com/gia-xang-dau/", headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        rows = []
        for row in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) >= 2:
                rows.append(cells)

        for keys, label in GRADES:
            for cells in rows:
                name = cells[0].lower()
                if not any(k in name for k in keys):
                    continue
                # Вùng 1 — базовая зона (крупные города и порты), Дананг в ней;
                # прочерк означает, что марка снята с продажи, а не сбой парсинга.
                for cell in cells[1:]:
                    m = re.search(r"\d{1,3}[.,]\d{3}", cell)
                    if not m:
                        continue
                    try:
                        val = int(m.group(0).replace(".", "").replace(",", ""))
                    except ValueError:
                        continue
                    if 15000 <= val <= 40000:
                        _update_price_cache("petrol", val, label)
                        log.info("Petrol: %s = %d ₫/л", label, val)
                        return val, None, label
        log.warning("Petrol: ни одна марка бензина не найдена с ценой на webgia.com")
    except (requests.RequestException, ValueError, AttributeError, TypeError) as e:
        log.warning("Petrol webgia fetch failed: %s", e)

    cached = _get_cached_price("petrol")
    if cached:
        value, fetched_at, label = cached
        # Запись старого формата (до появления label в кэше) — общая подпись.
        label = label or "бензин"
        log.warning("Petrol: источники недоступны, берём кэш от %s", fetched_at)
        return value, fetched_at, label
    log.warning("Petrol: нет ни свежих данных, ни валидного кэша — блок не публикуем")
    return None


def fetch_gold_price():
    """Возвращает (value: int, fetched_at: datetime|None, label: str) — цену ПРОДАЖИ
    золотого слитка SJC за chỉ (≈3.75 г), либо None, если нет ни свежих данных, ни
    валидного кэша. См. fetch_petrol_price про смысл fetched_at.

    Тонкость вёрстки webgia: таблица идёт по регионам, но реальные числа стоят
    только у первой строки (Хошимин), а у остальных городов — включая Дананг —
    вместо цен подставлена антискрейп-заглушка «xem tại webgia.com». Поэтому
    привязываться к Данангу бессмысленно: берём строку стандартного слитка
    «Vàng SJC 1L, 10L, 1KG» с настоящими числами. Цена слитка SJC по стране
    едина, так что котировка представительна.

    Из строки берём ВТОРОЕ число — «bán ra», цену продажи: именно её платит
    читатель, который идёт покупать. Раньше бралось первое попавшееся число,
    то есть цена покупки, а подпись в посте ей не соответствовала.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    }
    PRICE_RE = re.compile(r"\d{1,3}(?:[.,]\d{3}){2,}")

    for url in ("https://webgia.com/gia-vang/sjc/", "https://webgia.com/gia-vang/"):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for row in soup.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 3:
                    continue
                joined = " ".join(cells).lower()
                if "sjc" not in joined or "1l" not in joined:
                    continue

                prices = []
                for cell in cells:
                    m = PRICE_RE.search(cell)
                    if m:
                        try:
                            prices.append(int(m.group(0).replace(".", "").replace(",", "")))
                        except ValueError:
                            pass
                if len(prices) < 2:
                    # Строка-заглушка «xem tại webgia.com» — не сбой парсинга,
                    # у этого региона цены просто скрыты; идём дальше.
                    continue

                sell = prices[1]
                if 5_000_000 <= sell <= 25_000_000:
                    _update_price_cache("gold", sell, "SJC 9999 (продажа)")
                    log.info("Gold: SJC слиток, продажа = %d ₫/чи", sell)
                    return sell, None, "SJC 9999 (продажа)"

            log.warning("Gold: строка слитка SJC с ценами не найдена на %s", url)
        except (requests.RequestException, ValueError, AttributeError, TypeError) as e:
            log.warning("Gold fetch failed (%s): %s", url, e)

    # Попытка 2: DOJI XML (цены за chỉ × 1000, например 16,630 = 16.630.000).
    # Здесь Buy/Sell — именованные атрибуты (не regex по свободному тексту),
    # но таблица содержит и не-SJC золото (кольца, слитки других проб) —
    # фильтруем по Name, чтобы не подхватить чужую строку. Берём Sell (цена
    # продажи) — как и в основном источнике выше, иначе кэш и подпись
    # "продажа" молча разъезжаются с ценой покупки.
    try:
        resp = requests.get(
            "https://giavang.doji.vn/api/giavang/get-bang-gia-doji",
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        # XML вида <Row Name='SJC ...' Sell='16,880' Buy='16,630' />
        for row_match in re.finditer(r"<Row\b[^>]*/>", resp.text):
            tag = row_match.group(0)
            name_m = re.search(r"Name=['\"]([^'\"]+)['\"]", tag)
            sell_m = re.search(r"Sell=['\"]([\d,]+)['\"]", tag)
            if not name_m or not sell_m or "sjc" not in name_m.group(1).lower():
                continue
            raw = sell_m.group(1).replace(",", "")
            try:
                val = int(raw)
            except ValueError:
                continue
            # У DOJI цены в тысячах VND/chỉ (например 16630 → 16 630 000)
            if 5_000 <= val <= 25_000:
                _update_price_cache("gold", val * 1000, "SJC 9999 (продажа)")
                return val * 1000, None, "SJC 9999 (продажа)"
        log.warning("Gold: подходящие SJC-строки не найдены в DOJI XML")
    except (requests.RequestException, ValueError, AttributeError, TypeError) as e:
        log.warning("Gold DOJI fetch failed: %s", e)

    cached = _get_cached_price("gold")
    if cached:
        value, fetched_at, label = cached
        label = label or "SJC 9999 (продажа)"
        log.warning(
            "Gold: все источники недоступны/неоднозначны, используем кэш от %s: %d VND/чи",
            fetched_at.isoformat(), value,
        )
        return value, fetched_at, label

    log.warning(
        "Gold: источники недоступны и валидного кэша (не старше %d дней) нет — блок не публикуем",
        PRICE_CACHE_MAX_AGE_DAYS,
    )
    return None

# ---------------------------------------------------------------------------
# Хелпер форматирования VND
# ---------------------------------------------------------------------------
def format_vnd(n):
    """25350 -> '25 350' (пробел как разделитель тысяч)"""
    return f"{int(n):,}".replace(",", " ")

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
WEATHER_EMOJI = {
    0: "☀️",      # Clear
    1: "🌤️", 2: "🌤️", 3: "☁️",  # Partly cloudy / overcast
    45: "🌫️", 48: "🌫️",          # Fog
    51: "🌦️", 53: "🌦️", 55: "🌦️",  # Drizzle
    61: "🌧️", 63: "🌧️", 65: "🌧️",  # Rain
    71: "❄️", 73: "❄️", 75: "❄️", 77: "❄️",  # Snow
    80: "🌧️", 81: "🌧️", 82: "🌧️",  # Showers
    95: "⛈️", 96: "⛈️", 99: "⛈️",  # Thunderstorm
}

AQI_LEVELS = [
    (50, "🟢 Отлично"),
    (100, "🟡 Хорошо"),
    (150, "🟠 Средне"),
    (200, "🔴 Плохо"),
    (999, "🟣 Очень плохо"),
]

WIND_DIRS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]

DAY_NAMES_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
}

MONTH_NAMES_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


def weather_emoji(code):
    return WEATHER_EMOJI.get(code, "🌤️")


def wind_direction(degrees):
    idx = round(degrees / 45) % 8
    return WIND_DIRS[idx]


def aqi_level(value):
    for threshold, label in AQI_LEVELS:
        if value <= threshold:
            return label
    return "🟣 Очень плохо"


def _daily_value(d, key, i):
    """Безопасно достаёт d[key][i]. Open-Meteo регулярно кладёт null внутри
    daily-массивов (например precipitation_probability_max за пределами
    окна probability-модели) и иногда присылает daily короче ожидаемых 4
    дней — оба случая должны давать None, а не падать с IndexError/KeyError."""
    try:
        return d[key][i]
    except (KeyError, IndexError, TypeError):
        return None


def _fmt_temp(value):
    """round() падает на None — подставляем прочерк вместо обрыва форматирования."""
    return f"{round(value)}°" if value is not None else "—°"


def _fmt_percent(value):
    return f"{value}%" if value is not None else "—%"


def _build_forecast_days(d):
    """Возвращает список безопасных дневных прогнозов (максимум 3, начиная
    со следующего дня): [{"day_name", "emoji", "tmax", "tmin", "rain"}, ...].
    Дни без валидной даты пропускаются; если валидных дней не осталось —
    возвращается пустой список, и вызывающий код должен опустить блок
    «Прогноз» целиком, а не печатать пустой заголовок."""
    n_days = min(4, len(d.get("time") or []))
    days = []
    for i in range(1, n_days):
        date_str = _daily_value(d, "time", i)
        if not date_str:
            continue
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        days.append({
            "day_name": DAY_NAMES_RU[date.weekday()],
            "emoji": weather_emoji(_daily_value(d, "weather_code", i)),
            "tmax": _fmt_temp(_daily_value(d, "temperature_2m_max", i)),
            "tmin": _fmt_temp(_daily_value(d, "temperature_2m_min", i)),
            "rain": _fmt_percent(_daily_value(d, "precipitation_probability_max", i)),
        })
    return days


def _price_age_suffix(fetched_at, danang_tz):
    """Бензин/золото хранятся как (value, fetched_at) — fetched_at is None,
    если цена свежая, иначе это момент последнего успешного скрейпа (значение
    взято из кэша). Возвращает пометку с датой для кэшированных значений,
    чтобы не выдавать старую цифру за сегодняшнюю."""
    if not fetched_at:
        return ""
    local = fetched_at.astimezone(danang_tz)
    return f" (на {local.day} {MONTH_NAMES_RU[local.month]}, из кэша)"


def format_post(weather_data, aqi_data, fx_data=None, petrol=None, gold=None):
    c = weather_data["current"]
    d = weather_data["daily"]
    DANANG_TZ = timezone(timedelta(hours=7))
    now = datetime.now(DANANG_TZ)

    # Header emoji from current weather code
    header_emoji = weather_emoji(c["weather_code"])

    # Date
    day = now.day
    month = MONTH_NAMES_RU[now.month]

    # Current conditions
    temp = round(c["temperature_2m"])
    feels = round(c["apparent_temperature"])
    humidity = c["relative_humidity_2m"]
    wind_speed = c["wind_speed_10m"]
    wind_dir = wind_direction(c["wind_direction_10m"])

    # Forecast (skip today = index 0, take next 3 days). Если после фильтрации
    # невалидных/отсутствующих дней ничего не осталось — блок опускаем целиком,
    # а не печатаем пустой заголовок «📅 Прогноз:» без строк под ним.
    forecast_days = _build_forecast_days(d)
    if forecast_days:
        forecast_lines = [
            f"• {fd['day_name']}: {fd['emoji']} {fd['tmax']}/{fd['tmin']}, дождь {fd['rain']}"
            for fd in forecast_days
        ]
        forecast_section = "📅 Прогноз:\n" + "\n".join(forecast_lines) + "\n\n"
    else:
        forecast_section = ""

    # AQI — aqi_data может быть словарём с "aqi": None (станция без свежих
    # измерений, см. fetch_aqi), поэтому проверяем именно значение, а не
    # только truthiness самого словаря.
    if aqi_data and aqi_data.get("aqi") is not None:
        aqi_val = aqi_data["aqi"]
        try:
            # Доп. страховка: даже если сюда просочится ненормализованное
            # значение (например строка "-" в обход fetch_aqi), aqi_level()
            # не должен уронить весь пост — считаем это отсутствием данных.
            aqi_text = f"🫁 Воздух (AQI): {aqi_val} — {aqi_level(aqi_val)}"
        except TypeError:
            aqi_text = "🫁 Воздух (AQI): нет данных"
    else:
        aqi_text = "🫁 Воздух (AQI): нет данных"

    # Дополнительные блоки (валюта/бензин/золото) — каждый опционален.
    # petrol/gold — это (value, fetched_at); fetched_at не None, если цена
    # взята из кэша (все источники сегодня недоступны) — тогда явно
    # указываем возраст цифры, а не выдаём её за сегодняшнюю.
    extra_blocks = []
    if fx_data:
        extra_blocks.append(
            "💱 Курсы валют:\n"
            f"• 1 USD ≈ {format_vnd(round(fx_data['usd_to_vnd']))} VND\n"
            f"• 1 RUB ≈ {format_vnd(round(fx_data['rub_to_vnd']))} VND"
        )
    if petrol:
        # Марка приходит из источника: рынок перешёл на E10, и подписывать всё
        # подряд как «A95» значило бы врать читателю про то, что он заливает.
        petrol_value, petrol_fetched_at, petrol_label = petrol
        extra_blocks.append(
            f"⛽ Бензин {petrol_label}: {format_vnd(petrol_value)} ₫/л"
            f"{_price_age_suffix(petrol_fetched_at, DANANG_TZ)}"
        )
    if gold:
        gold_value, gold_fetched_at, gold_label = gold
        extra_blocks.append(
            f"🥇 Золото {gold_label}: {format_vnd(gold_value)} ₫/чи"
            f"{_price_age_suffix(gold_fetched_at, DANANG_TZ)}"
        )

    extra_text = ""
    if extra_blocks:
        extra_text = "\n\n" + "\n\n".join(extra_blocks)

    post = (
        f"{header_emoji} ПОГОДА В ДАНАНГЕ — {day} {month}\n"
        f"\n"
        f"🌡 Сейчас: +{temp}°C, ощущается как +{feels}°C\n"
        f"💧 Влажность: {humidity}%\n"
        f"💨 Ветер: {wind_speed} км/ч, {wind_dir}\n"
        f"\n"
        f"{forecast_section}"
        f"{aqi_text}"
        f"{extra_text}\n"
        f"\n"
        f"#Дананг #погода #Danang #weather #Vietnam"
    )
    return post


def format_post_rich(weather_data, aqi_data, fx_data=None, petrol=None, gold=None, style="table"):
    """Тот же дайджест в Rich HTML (Bot API 10.1+).

    style="table" — показатели таблицей, компактно и с выравниванием цифр;
    style="list"  — то же списками, если таблицы окажутся тесными на телефоне.
    """
    c = weather_data["current"]
    d = weather_data["daily"]
    DANANG_TZ = timezone(timedelta(hours=7))
    now = datetime.now(DANANG_TZ)

    header_emoji = weather_emoji(c["weather_code"])
    day = now.day
    month = MONTH_NAMES_RU[now.month]
    temp = round(c["temperature_2m"])
    feels = round(c["apparent_temperature"])
    humidity = c["relative_humidity_2m"]
    wind_speed = c["wind_speed_10m"]
    wind_dir = wind_direction(c["wind_direction_10m"])

    rows = [
        ("🌡 Сейчас", f"+{temp}°C"),
        ("🤔 Ощущается", f"+{feels}°C"),
        ("💧 Влажность", f"{humidity}%"),
        ("💨 Ветер", f"{wind_speed} км/ч, {wind_dir}"),
    ]

    forecast_days = _build_forecast_days(d)
    forecast = [
        (fd["day_name"], f"{fd['emoji']} {fd['tmax']}/{fd['tmin']}", f"дождь {fd['rain']}")
        for fd in forecast_days
    ]

    digest = []
    if fx_data:
        digest.append(("💵 1 USD", f"{format_vnd(round(fx_data['usd_to_vnd']))} ₫"))
        digest.append(("🇷🇺 1 RUB", f"{format_vnd(round(fx_data['rub_to_vnd']))} ₫"))
    if petrol:
        petrol_value, petrol_fetched_at, petrol_label = petrol
        digest.append((f"⛽ Бензин {petrol_label}",
                       f"{format_vnd(petrol_value)} ₫/л{_price_age_suffix(petrol_fetched_at, DANANG_TZ)}"))
    if gold:
        gold_value, gold_fetched_at, gold_label = gold
        digest.append((f"🥇 Золото {gold_label}",
                       f"{format_vnd(gold_value)} ₫/чи{_price_age_suffix(gold_fetched_at, DANANG_TZ)}"))

    def kv_table(pairs):
        body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in pairs)
        return f"<table striped>{body}</table>"

    def kv_list(pairs):
        return "<ul>" + "".join(f"<li>{k}: <b>{v}</b></li>" for k, v in pairs) + "</ul>"

    kv = kv_table if style == "table" else kv_list

    parts = [f"<h3>{header_emoji} ПОГОДА В ДАНАНГЕ — {day} {month}</h3>", kv(rows)]

    # Если после фильтрации невалидных/отсутствующих дней прогноза не
    # осталось — опускаем блок целиком (не печатаем заголовок над пустотой).
    if forecast:
        parts.append("<p><b>📅 Прогноз на три дня</b></p>")
        if style == "table":
            body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c_}</td></tr>" for a, b, c_ in forecast)
            parts.append(f"<table striped>{body}</table>")
        else:
            parts.append("<ul>" + "".join(f"<li>{a}: <b>{b}</b>, {c_}</li>" for a, b, c_ in forecast) + "</ul>")

    if aqi_data and aqi_data.get("aqi") is not None:
        try:
            # См. комментарий в format_post — защита от ненормализованного aqi.
            parts.append(f"<blockquote>🫁 Воздух (AQI): <b>{aqi_data['aqi']}</b> — "
                         f"{aqi_level(aqi_data['aqi'])}</blockquote>")
        except TypeError:
            parts.append("<blockquote>🫁 Воздух (AQI): нет данных</blockquote>")
    else:
        parts.append("<blockquote>🫁 Воздух (AQI): нет данных</blockquote>")

    if digest:
        parts.append("<hr/>")
        parts.append("<p><b>💱 Курсы и цены</b></p>")
        parts.append(kv(digest))

    parts.append("<footer>#Дананг #погода #Danang #weather #Vietnam</footer>")
    return "".join(parts)


# Примечание: старую send_telegram() убрали — после перехода на общий
# telegram_sender.py (send_telegram_message/send_rich_message) она нигде не
# вызывалась, дублировала retry-логику и, вдобавок, светила токен бота в лог.

# ---------------------------------------------------------------------------
# Лок и защита от повторной публикации
# ---------------------------------------------------------------------------
# Держим ссылку на дескриптор лока на уровне модуля: если оставить её только
# локальной переменной внутри main() и не использовать дальше, сборщик мусора
# рано или поздно закроет файл и снимет flock ещё до конца процесса.
_lock_fh = None


def acquire_lock(lock_path):
    """Неблокирующий файловый лок: вторая одновременно запущенная копия
    weather_bot читала бы тот же price_cache.json/heartbeats.json и могла
    столкнуться при записи или опубликовать дубль. Лок держится открытым до
    конца процесса — ОС снимает его автоматически при завершении, даже при
    аварийном выходе. (Тот же паттерн, что acquire_lock в expat_guide_bot.py.)
    """
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info(
            "Другая копия weather_bot уже выполняется (занят lock-файл %s) — выходим с кодом 0",
            lock_path,
        )
        sys.exit(0)
    return lock_file


def _already_posted_today_danang(heartbeat_path=HEARTBEAT_PATH, rubric="weather"):
    """True, если последняя успешная публикация рубрики (heartbeats.json,
    см. record_heartbeat в telegram_sender.py) приходится на сегодняшнюю дату
    по времени Дананга (UTC+7).

    Файл читается только на чтение. Любая аномалия — файла нет, битый JSON,
    неожиданная форма записи (нет ключа rubric/last_posted_at, не строка и
    т.п.) — трактуется как "публикации сегодня не было": защита от дубля не
    должна сама по себе останавливать публикацию из-за постороннего сбоя.
    """
    try:
        with open(heartbeat_path, encoding="utf-8") as f:
            data = json.load(f)
        last_posted_at = data[rubric]["last_posted_at"]
        last_dt = datetime.fromisoformat(last_posted_at)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    danang_tz = timezone(timedelta(hours=7))
    return last_dt.astimezone(danang_tz).date() == datetime.now(danang_tz).date()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Разбор и валидация аргументов — самое первое действие в main(), до
    # лока/сети/чтения state. Неизвестный флаг обычно значит опечатку в
    # cron/руках оператора; лучше явно упасть с usage, чем молча проигнорировать
    # его и запустить публикацию не в том режиме, который имелся в виду.
    allowed_args = {"--dry-run", "--test", "--force", "--plain", "--rich-list"}
    unknown_args = [a for a in sys.argv[1:] if a not in allowed_args]
    if unknown_args:
        print(f"Usage: {Path(sys.argv[0]).name} [--dry-run] [--test] [--force] [--plain] [--rich-list]",
              file=sys.stderr)
        print(f"Неизвестный аргумент(ы): {' '.join(unknown_args)}", file=sys.stderr)
        sys.exit(2)

    log.info("=== Weather bot start ===")
    dry_run = "--dry-run" in sys.argv
    test_mode = "--test" in sys.argv
    force = "--force" in sys.argv
    global _DRY_RUN
    _DRY_RUN = dry_run
    # Rich-разметка — рабочий формат дайджеста (таблицы показателей, цитата с AQI,
    # разделитель перед финансовой частью). --plain оставлен как аварийный откат
    # на случай проблем с sendRichMessage.
    rich_mode = "--plain" not in sys.argv
    rich_style = "list" if "--rich-list" in sys.argv else "table"
    if dry_run:
        log.info("DRY RUN mode — Telegram/Facebook отправка отключена")
    if test_mode:
        log.info("TEST mode — постим в тестовую группу, Facebook пропускаем")
    if force:
        log.info("FORCE mode — игнорируем защиту от повторной публикации за сегодня")
    if rich_mode:
        log.info("RICH mode — sendRichMessage, стиль %s", rich_style)

    # Неблокирующий лок против второй одновременно запущенной копии — не берём
    # его для --dry-run, этот режим ничего не пишет и не публикует, мешать
    # боевому запуску незачем.
    if not dry_run:
        global _lock_fh
        _lock_fh = acquire_lock(LOCK_PATH)

    # Защита от повторной публикации в те же сутки по времени Дананга — если
    # только не просят явно (--force) или это тестовый/пробный прогон.
    if not (force or test_mode or dry_run) and _already_posted_today_danang():
        log.info("Погода уже была опубликована сегодня (Дананг, UTC+7) — выходим с кодом 0")
        sys.exit(0)

    cfg = load_config()

    # Fetch data
    log.info("Fetching weather data...")
    try:
        weather_data = fetch_weather(cfg)
    except Exception as e:
        log.error("Failed to fetch weather: %s", e)
        sys.exit(1)
    log.info("Weather data OK")

    log.info("Fetching AQI data...")
    try:
        aqi_data = fetch_aqi(cfg)
        log.info("AQI data OK: %s", aqi_data.get("aqi") if aqi_data else "N/A")
    except Exception as e:
        log.warning("AQI fetch failed: %s", e)
        aqi_data = None

    log.info("Fetching exchange rates...")
    try:
        fx_data = fetch_exchange_rates()
        if fx_data:
            log.info(
                "FX OK: usd_to_vnd=%s, rub_to_vnd=%s",
                fx_data["usd_to_vnd"], fx_data["rub_to_vnd"],
            )
        else:
            log.warning("FX returned None")
    except Exception as e:
        log.warning("FX fetch failed: %s", e)
        fx_data = None

    log.info("Fetching petrol price...")
    try:
        petrol = fetch_petrol_price()  # (value, fetched_at, label) | None
        if petrol:
            value, fetched_at, label = petrol
            if fetched_at:
                log.info("Petrol OK (%s, из кэша от %s): %s VND/л", label, fetched_at.isoformat(), value)
            else:
                log.info("Petrol OK (%s): %s VND/л", label, value)
        else:
            log.warning("Petrol: нет ни свежих данных, ни валидного кэша — блок не публикуем")
    except Exception as e:
        log.warning("Petrol fetch failed: %s", e)
        petrol = None

    log.info("Fetching gold price...")
    try:
        gold = fetch_gold_price()  # (value, fetched_at, label) | None
        if gold:
            value, fetched_at, label = gold
            if fetched_at:
                log.info("Gold OK (%s, из кэша от %s): %s VND/чи", label, fetched_at.isoformat(), value)
            else:
                log.info("Gold OK (%s): %s VND/чи", label, value)
        else:
            log.warning("Gold: нет ни свежих данных, ни валидного кэша — блок не публикуем")
    except Exception as e:
        log.warning("Gold fetch failed: %s", e)
        gold = None

    # Format. Второстепенный источник (AQI, курсы, бензин, золото) не должен
    # ронять публикацию погоды целиком — если format_post всё же упал на
    # чём-то неучтённом, откатываемся на минимальный пост без доп. блоков,
    # а не оставляем сообщество без прогноза на день.
    try:
        post_text = format_post(weather_data, aqi_data, fx_data=fx_data, petrol=petrol, gold=gold)
    except Exception as e:
        log.error("format_post упал: %s — публикуем минимальный пост (только погода)", e)
        try:
            post_text = format_post(weather_data, None, fx_data=None, petrol=None, gold=None)
        except Exception as e2:
            log.error("Минимальный format_post тоже упал: %s — публикация невозможна", e2)
            sys.exit(1)
    log.info("Post formatted (%d chars)", len(post_text))

    # send_rich — рабочий флаг для этого запуска: если rich-форматирование
    # упадёт, откатываемся на обычный текст, а не теряем весь пост.
    send_rich = rich_mode
    rich_html = None
    if send_rich:
        try:
            rich_html = format_post_rich(weather_data, aqi_data, fx_data=fx_data,
                                         petrol=petrol, gold=gold, style=rich_style)
            log.info("Rich post formatted (%d chars, style=%s)", len(rich_html), rich_style)
        except Exception as e:
            log.warning("format_post_rich упал: %s — используем обычный текст вместо rich-версии", e)
            rich_html = None
            send_rich = False

    # Печатаем пост (всегда — удобно и для боевого, и для dry-run)
    print("=" * 60)
    print(rich_html or post_text)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN: пропускаем отправку, выходим")
        return

    # Send
    thread_id = cfg["telegram"].get("weather_thread_id")
    try:
        if send_rich:
            msg_id = send_rich_message(cfg, rich_html, thread_id=thread_id, test=test_mode,
                                       rubric="weather")
            if msg_id is None:
                # sendRichMessage может быть недоступен на боевом API или отвергнуть
                # разметку — plain-текст уже сформирован, откатываемся на него,
                # а не теряем публикацию целиком.
                log.warning("sendRichMessage вернул None — откатываемся на обычный текст (plain fallback)")
                msg_id = send_telegram_message(cfg, post_text, thread_id=thread_id, test=test_mode,
                                              rubric="weather")
        else:
            msg_id = send_telegram_message(cfg, post_text, thread_id=thread_id, test=test_mode,
                                          rubric="weather")
    except SendOutcomeUnknown as e:
        # Запрос мог реально дойти до Telegram (таймаут на чтении/5xx/нечитаемый
        # ответ) — повтор или plain-fallback рискуют задвоить пост в живой
        # группе. Останавливаемся немедленно, без второй попытки.
        log.error("Telegram: исход отправки неизвестен, не повторяем во избежание дубля: %s", e)
        sys.exit(1)
    if not msg_id:
        log.error("Failed to send to Telegram")
        sys.exit(1)

    # В тестовом режиме на Facebook ничего не публикуем — это боевой канал
    if not test_mode:
        send_facebook_post(cfg, post_text)


if __name__ == "__main__":
    main()
