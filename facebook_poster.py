"""Post to Facebook Page via Graph API."""

import logging
import time

import requests

log = logging.getLogger(__name__)


def send_facebook_post(cfg, text):
    """Post text to Facebook Page. Returns post ID string or None."""
    fb = cfg.get("facebook")
    if not fb:
        log.warning("Facebook config missing — skipping FB post")
        return None

    page_id = fb["page_id"]
    access_token = fb["page_access_token"]
    url = f"https://graph.facebook.com/v22.0/{page_id}/feed"
    payload = {
        "message": text,
        "access_token": access_token,
    }

    backoff = [5, 30, 60]
    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=15)
            result = resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("Facebook response not JSON: %s", resp.text[:500])
            return None
        except requests.exceptions.RequestException as e:
            log.error("Facebook request failed: %s", e)
            if attempt < 2:
                time.sleep(backoff[attempt])
                continue
            return None

        if "id" in result:
            post_id = result["id"]
            log.info("Facebook post sent: post_id=%s", post_id)
            return post_id

        error = result.get("error", {})
        error_code = error.get("code")
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
