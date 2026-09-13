"""Post to Facebook Page via Graph API."""

import logging
import time

import requests

log = logging.getLogger(__name__)



# Постоянные (не проходящие сами по себе) ошибки Graph API — ретраить их
# бессмысленно, мёртвый токен не оживёт от повторной попытки. Известный
# случай в этом проекте: токен мёртв с 7 мая, за 107 дней накопилось 249
# одинаковых записей в логах, потому что раньше эти коды тонули в общем
# log.error и никто не заметил, что страница перестала получать посты.
#   190 — access token истёк / невалиден
#   200 — недостаточно прав на страницу
#   10  — permission denied (feature/permission отозвана)
_PERMANENT_ERROR_CODES = {190, 200, 10}


def send_facebook_post(cfg, text):
    """Post text to Facebook Page. Returns post ID string or None.

    Всё тело обёрнуто в try/except Exception: в новостном боте эта функция
    вызывается ДО обновления трекера дедупликации, поэтому любое
    необработанное исключение здесь оставит трекер необновлённым и та же
    новость выйдет повторно на следующем прогоне. Постинг в Facebook —
    побочная, необязательная часть пайплайна и не должен уметь ронять его
    ни при каких обстоятельствах.
    """
    try:
        fb = cfg.get("facebook", {})
        # Явный переключатель "включено": по умолчанию (секция отсутствует,
        # пуста, не словарь, enabled отсутствует/не булево True) — выключено.
        # Раньше отключал только явный enabled=False, а отсутствующий ключ
        # считался "включено" — из-за этого с мёртвым токеном (с 7 мая) бот
        # 107 дней подряд слал запросы на FB, которые заведомо не пройдут.
        if not isinstance(fb, dict) or fb.get("enabled") is not True:
            log.info("Facebook отключён в конфиге (facebook.enabled не true) — пропускаем")
            return None

        # Секция может присутствовать, но быть неполной (например, только
        # page_id без токена) — прямая индексация fb["..."] раньше падала
        # KeyError и пробивала исключение наружу до вызывающего кода.
        page_id = fb.get("page_id")
        access_token = fb.get("page_access_token")
        if not page_id or not access_token:
            log.warning(
                "Facebook config incomplete (page_id/page_access_token) — skipping FB post"
            )
            return None

        url = f"https://graph.facebook.com/v22.0/{page_id}/feed"
        payload = {
            "message": text,
            "access_token": access_token,
        }

        backoff = [5, 30, 60]
        for attempt in range(3):
            try:
                resp = requests.post(url, data=payload, timeout=15)
            except requests.exceptions.RequestException as e:
                # Сетевой сбой на попытках 1-2 — рабочий случай, не авария:
                # логируем как warning и ретраим. error оставляем только
                # последней попытке, когда ретраи закончились.
                if attempt < 2:
                    log.warning(
                        "Facebook request failed (attempt %d/3): %s", attempt + 1, e
                    )
                    time.sleep(backoff[attempt])
                    continue
                log.error("Facebook request failed (attempt 3/3): %s", e)
                return None

            if resp.status_code >= 500:
                # 5xx у Graph API обычно приходит не в JSON (html-страница
                # ошибки), поэтому даже не пытаемся парсить тело — сразу
                # ретраим по той же схеме, что и rate limit.
                if attempt < 2:
                    log.warning(
                        "Facebook server error %d, retry %d/3 after %ds",
                        resp.status_code, attempt + 1, backoff[attempt],
                    )
                    time.sleep(backoff[attempt])
                    continue
                log.error(
                    "Facebook server error %d on attempt 3/3 — giving up",
                    resp.status_code,
                )
                return None

            try:
                result = resp.json()
            except requests.exceptions.JSONDecodeError:
                log.error("Facebook response not JSON: %s", resp.text[:500])
                return None

            if "id" in result:
                post_id = result["id"]
                log.info("Facebook post sent: post_id=%s", post_id)
                return post_id

            error = result.get("error", {})
            error_code = error.get("code")

            if error_code in _PERMANENT_ERROR_CODES:
                log.error(
                    "Facebook постоянная ошибка (code=%s): %s — требуется "
                    "ручное вмешательство (перевыпуск page access token), "
                    "ретраи не помогут",
                    error_code, error.get("message", result),
                )
                return None

            if error_code == 4:
                wait = backoff[attempt]
                log.warning("Facebook rate limit, retry %d/3 after %ds", attempt + 1, wait)
                if attempt < 2:
                    time.sleep(wait)
                    continue
                else:
                    log.error("Facebook rate limit on attempt 3/3 — giving up")
                    return None

            log.error("Facebook API error: %s", error.get("message", result))
            return None

        return None
    except Exception:
        log.exception(
            "Unexpected error posting to Facebook — swallowed so the main pipeline continues"
        )
        return None
