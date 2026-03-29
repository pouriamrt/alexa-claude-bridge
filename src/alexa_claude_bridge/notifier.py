"""Send results back to Alexa proactively via Notify Me skill."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

NOTIFY_ME_URL = "https://api.notifymyecho.com/v1/NotifyMe"


def notify_alexa(summary: str, access_code: str) -> bool:
    """Push a spoken notification to Alexa via the Notify Me skill.

    The user hears: "Notify Me: <summary>"
    Requires a free access code from https://www.notifymyecho.com

    Returns True if notification was sent successfully.
    """
    if not access_code:
        logger.debug("No NOTIFY_ME_ACCESS_CODE configured — skipping Alexa notification")
        return False

    try:
        resp = httpx.post(
            NOTIFY_ME_URL,
            json={
                "notification": summary,
                "accessCode": access_code,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Alexa notification sent: %s", summary[:80])
            return True
        logger.warning("Notify Me API returned %d: %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError as exc:
        logger.warning("Failed to send Alexa notification: %s", exc)
        return False
