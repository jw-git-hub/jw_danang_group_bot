#!/usr/bin/env python3
"""
Ежедневный прогноз погоды и AQI для Дананга → Telegram.
Запуск: python3 weather_bot.py
Cron: 0 7 * * * cd /path/to/danang-bots && python3 weather_bot.py >> logs/weather.log 2>&1
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from facebook_poster import send_facebook_post

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
            return data["data"]
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                log.warning("AQI fetch attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, backoff[attempt])
                time.sleep(backoff[attempt])
            else:
                log.error("AQI fetch attempt 3/3 failed: %s — giving up", e)
                raise

# ---------------------------------------------------------------------------
# Курсы валют (open.er-api.com — бесплатно, без ключа; frankfurter — fallback)
# ---------------------------------------------------------------------------
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
            return {
                "usd_to_vnd": float(vnd),
                "rub_to_vnd": float(vnd) / float(rub),
            }
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
            return {
                "usd_to_vnd": usd_to_vnd,
                "rub_to_vnd": usd_to_vnd / usd_to_rub,
            }
        log.warning("FX fallback: отсутствуют курсы в ответе frankfurter: %s", list(rates)[:10])
    except requests.exceptions.RequestException as e:
        log.warning("FX fallback fetch failed: %s", e)
    except (ValueError, KeyError, TypeError) as e:
        log.warning("FX fallback parse failed: %s", e)

    return None

# ---------------------------------------------------------------------------
# Цена бензина RON 95-III (webgia.com — агрегатор Petrolimex)
# ---------------------------------------------------------------------------
# Static fallback: цена RON 95-III из последней публикации (на случай если
# все источники недоступны). Лучше иметь приближённое значение, чем пустоту.
PETROL_FALLBACK_VND = 22880  # обновлять вручную при больших изменениях

def fetch_petrol_price():
    """Возвращает int (VND/литр) для RON 95-III. None — никогда (есть fallback).

    Источники:
      1. https://webgia.com/gia-xang-dau/  — таблица Petrolimex (стабильно).
      2. https://www.petrolimex.com.vn/nd/gia-xang-dau/  — официальный сайт
         (вёрстка меняется, парсинг хрупкий).
    """
    import re

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    }

    # Попытка 1: webgia.com — там есть строка "Xăng RON 95-III" с ценой VND/л
    try:
        resp = requests.get("https://webgia.com/gia-xang-dau/", headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("tr"):
            text = row.get_text(" ", strip=True)
            if "RON 95-III" not in text:
                continue
            # Числа вида 22.880 (точка как разделитель тысяч)
            for c in re.findall(r"\d{2}[.,]\d{3}", text):
                raw = c.replace(".", "").replace(",", "")
                try:
                    val = int(raw)
                except ValueError:
                    continue
                if 15000 <= val <= 40000:
                    return val
        log.warning("Petrol: строка RON 95-III не найдена на webgia.com")
    except Exception as e:
        log.warning("Petrol webgia fetch failed: %s", e)

    # Попытка 2: petrolimex.com.vn (хрупко — вёрстка может не содержать таблицу)
    try:
        resp = requests.get(
            "https://www.petrolimex.com.vn/nd/gia-xang-dau/",
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("tr"):
            text = row.get_text(" ", strip=True)
            lower = text.lower()
            if "ron 95-iii" not in lower and "ron 95 iii" not in lower:
                continue
            for c in re.findall(r"\d{1,3}(?:[.,]\d{3})+|\d{5,6}", text):
                raw = c.replace(".", "").replace(",", "")
                try:
                    val = int(raw)
                except ValueError:
                    continue
                if 15000 <= val <= 40000:
                    return val
        log.warning("Petrol: строка RON 95-III не найдена на petrolimex.com.vn")
    except Exception as e:
        log.warning("Petrol petrolimex fetch failed: %s", e)

    log.warning(
        "Petrol: все источники недоступны. Используется fallback значение %d VND/л",
        PETROL_FALLBACK_VND,
    )
    return PETROL_FALLBACK_VND

# ---------------------------------------------------------------------------
# Цена золота SJC 9999 (webgia.com — агрегатор; sjc.com.vn блокирует ботов)
# ---------------------------------------------------------------------------
# Static fallback (VND/chỉ). Обновлять при больших движениях рынка.
GOLD_FALLBACK_VND = 16_630_000

def fetch_gold_price():
    """Возвращает int (VND за chỉ ≈3.75г). None — никогда (есть fallback).

    Источники:
      1. https://webgia.com/gia-vang/  — таблица SJC по городам, đơn vị: đồng/chỉ.
      2. https://giavang.doji.vn/api/giavang/get-bang-gia-doji  — XML API.
    """
    import re

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    }

    # Попытка 1: webgia.com — таблица "Tổng hợp Giá vàng SJC trên Toàn Quốc"
    try:
        resp = requests.get("https://webgia.com/gia-vang/", headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("tr"):
            text = row.get_text(" ", strip=True)
            if "SJC" not in text:
                continue
            # Цена за chỉ вида 16.630.000
            candidates = re.findall(r"\d{1,3}(?:[.,]\d{3}){2,}", text)
            for c in candidates:
                raw = c.replace(".", "").replace(",", "")
                try:
                    val = int(raw)
                except ValueError:
                    continue
                # Допустимый диапазон ~5–25 млн VND/chỉ (с запасом)
                if 5_000_000 <= val <= 25_000_000:
                    return val
        log.warning("Gold: строка SJC не найдена на webgia.com")
    except Exception as e:
        log.warning("Gold webgia fetch failed: %s", e)

    # Попытка 2: DOJI XML (цены за chỉ × 1000, например 16,630 = 16.630.000)
    try:
        resp = requests.get(
            "https://giavang.doji.vn/api/giavang/get-bang-gia-doji",
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        # XML вида <Row Name='...' Sell='16,880' Buy='16,630' />
        for m in re.finditer(r"Buy=['\"]([\d,]+)['\"]", resp.text):
            raw = m.group(1).replace(",", "")
            try:
                val = int(raw)
            except ValueError:
                continue
            # У DOJI цены в тысячах VND/chỉ (например 16630 → 16 630 000)
            if 5_000 <= val <= 25_000:
                return val * 1000
        log.warning("Gold: подходящие значения не найдены в DOJI XML")
    except Exception as e:
        log.warning("Gold DOJI fetch failed: %s", e)

    log.warning(
        "Gold: все источники недоступны. Используется fallback значение %d VND/чи",
        GOLD_FALLBACK_VND,
    )
    return GOLD_FALLBACK_VND

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

    # Forecast (skip today = index 0, take next 3 days)
    forecast_lines = []
    for i in range(1, 4):
        date = datetime.strptime(d["time"][i], "%Y-%m-%d")
        day_name = DAY_NAMES_RU[date.weekday()]
        emoji = weather_emoji(d["weather_code"][i])
        tmax = round(d["temperature_2m_max"][i])
        tmin = round(d["temperature_2m_min"][i])
        rain = d["precipitation_probability_max"][i]
        forecast_lines.append(f"• {day_name}: {emoji} {tmax}°/{tmin}°, дождь {rain}%")

    forecast_block = "\n".join(forecast_lines)

    # AQI
    if aqi_data:
        aqi_val = aqi_data["aqi"]
        aqi_text = f"🫁 Воздух (AQI): {aqi_val} — {aqi_level(aqi_val)}"
    else:
        aqi_text = "🫁 Воздух (AQI): нет данных"

    # Дополнительные блоки (валюта/бензин/золото) — каждый опционален
    extra_blocks = []
    if fx_data:
        extra_blocks.append(
            "💱 Курсы валют:\n"
            f"• 1 USD ≈ {format_vnd(round(fx_data['usd_to_vnd']))} VND\n"
            f"• 1 RUB ≈ {format_vnd(round(fx_data['rub_to_vnd']))} VND"
        )
    if petrol:
        extra_blocks.append(f"⛽ Бензин A95: {format_vnd(petrol)} ₫/л")
    if gold:
        extra_blocks.append(f"🥇 Золото SJC 9999: {format_vnd(gold)} ₫/чи")

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
        f"📅 Прогноз:\n"
        f"{forecast_block}\n"
        f"\n"
        f"{aqi_text}"
        f"{extra_text}\n"
        f"\n"
        f"#Дананг #погода #Danang #weather #Vietnam"
    )
    return post

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(cfg, text):
    token = cfg["telegram"]["bot_token"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cfg["telegram"]["chat_id"],
        "text": text,
    }
    thread_id = cfg["telegram"].get("weather_thread_id")
    if thread_id and thread_id != 1:
        payload["message_thread_id"] = thread_id

    backoff = [5, 15, 30]
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            # Handle Telegram 429 (rate limit)
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", backoff[attempt])
                log.warning("Telegram 429, retry_after=%ds (attempt %d/3)", retry_after, attempt + 1)
                if attempt < 2:
                    time.sleep(retry_after)
                    continue
                else:
                    log.error("Telegram 429 on attempt 3/3 — giving up")
                    return None
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("Telegram response not JSON: %s", resp.text[:500])
            return None
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                log.warning("Telegram send attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, backoff[attempt])
                time.sleep(backoff[attempt])
                continue
            else:
                log.error("Telegram send attempt 3/3 failed: %s — giving up", e)
                return None

        if not result.get("ok"):
            log.error("Telegram API error: %s", result)
            return None
        msg_id = result["result"]["message_id"]
        log.info("Telegram: sent message_id=%s to thread=%s", msg_id, payload.get("message_thread_id", "General"))
        return msg_id
    return None

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== Weather bot start ===")
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info("DRY RUN mode — Telegram/Facebook отправка отключена")
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
        petrol = fetch_petrol_price()
        if petrol:
            log.info("Petrol OK: %s VND/л", petrol)
        else:
            log.warning("Petrol returned None")
    except Exception as e:
        log.warning("Petrol fetch failed: %s", e)
        petrol = None

    log.info("Fetching gold price...")
    try:
        gold = fetch_gold_price()
        if gold:
            log.info("Gold OK: %s VND/чи", gold)
        else:
            log.warning("Gold returned None")
    except Exception as e:
        log.warning("Gold fetch failed: %s", e)
        gold = None

    # Format
    post_text = format_post(weather_data, aqi_data, fx_data=fx_data, petrol=petrol, gold=gold)
    log.info("Post formatted (%d chars)", len(post_text))

    # Печатаем пост (всегда — удобно и для боевого, и для dry-run)
    print("=" * 60)
    print(post_text)
    print("=" * 60)

    if dry_run:
        log.info("DRY RUN: пропускаем отправку, выходим")
        return

    # Send
    msg_id = send_telegram(cfg, post_text)
    if not msg_id:
        log.error("Failed to send to Telegram")
        sys.exit(1)

    send_facebook_post(cfg, post_text)


if __name__ == "__main__":
    main()
