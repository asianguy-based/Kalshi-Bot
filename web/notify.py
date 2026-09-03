"""Outbound notifications, so an unattended run is not a silent one.

The engine runs headless on a server. Without this, a tripped circuit
breaker or an authentication failure is invisible until someone happens to
open the dashboard - which defeats the point of running it unattended.

Transport is a webhook (Discord, Slack, or any URL that accepts a JSON POST)
plus optional email via an SMTP relay. Both are configured through env vars
and both are optional: with nothing configured, notify() is a no-op and the
engine behaves exactly as before.

Design notes:
  * Never raises. A notification failure must not kill a trading loop or
    turn into a retry storm, so every path is wrapped and logged.
  * Rate-limited per event key. A halt condition can re-fire every poll
    interval; without throttling, one bad afternoon is 5000 messages.
  * Never includes credentials. The message body is built from a fixed set
    of fields, not from arbitrary config, so a key cannot leak into a
    third-party webhook.
"""

import os
import json
import time
import logging
import smtplib
import threading
import urllib.error
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger("KalshiBot")

WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
SMTP_HOST = os.environ.get("NOTIFY_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("NOTIFY_SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("NOTIFY_SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("NOTIFY_SMTP_PASS", "")
EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "").strip() or SMTP_USER
LABEL = os.environ.get("NOTIFY_LABEL", "kalshi-node").strip()

# Minimum seconds between two notifications sharing the same key.
DEFAULT_THROTTLE = int(os.environ.get("NOTIFY_THROTTLE_SECONDS", "900") or 900)

_last_sent = {}
_lock = threading.Lock()

# Severity ordering, so a future config can set a floor.
LEVELS = {"info": 10, "warning": 20, "critical": 30}
MIN_LEVEL = LEVELS.get(os.environ.get("NOTIFY_MIN_LEVEL", "info").lower(), 10)


def enabled():
    return bool(WEBHOOK_URL or (SMTP_HOST and EMAIL_TO))


def _throttled(key, throttle):
    """True if this key fired too recently. Records the send time if not."""
    now = time.time()
    with _lock:
        last = _last_sent.get(key, 0.0)
        if now - last < throttle:
            return True
        _last_sent[key] = now
        return False


def _post_webhook(title, body, level):
    # Discord and Slack both accept a bare {"content"/"text": "..."} payload,
    # so send both keys and let the receiver pick the one it understands.
    text = f"**[{LABEL}] {title}**\n{body}"
    payload = json.dumps({"content": text, "text": text}).encode()
    req = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "kalshi-node/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return 200 <= resp.status < 300


def _send_email(title, body, level):
    msg = EmailMessage()
    msg["Subject"] = f"[{LABEL}] {title}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except smtplib.SMTPException:
            # Relay without STARTTLS support; sending is still better than
            # silence, and the body carries no credentials.
            logger.warning("SMTP relay did not offer STARTTLS.")
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


def notify(title, body="", level="info", key=None, throttle=None):
    """Fire a notification. Returns True if anything was actually sent.

    key      - throttling identity; defaults to the title, so repeated
               identical alerts collapse into one per window.
    throttle - seconds; defaults to NOTIFY_THROTTLE_SECONDS.
    """
    if not enabled():
        return False
    if LEVELS.get(level, 10) < MIN_LEVEL:
        return False

    key = key or title
    throttle = DEFAULT_THROTTLE if throttle is None else throttle
    if throttle and _throttled(key, throttle):
        logger.debug("Notification throttled: %s", key)
        return False

    sent = False
    if WEBHOOK_URL:
        try:
            sent = _post_webhook(title, body, level) or sent
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            logger.error("Webhook notification failed: %s", exc)
        except Exception as exc:                       # never propagate
            logger.error("Webhook notification error: %s", exc)

    if SMTP_HOST and EMAIL_TO:
        try:
            sent = _send_email(title, body, level) or sent
        except Exception as exc:
            logger.error("Email notification failed: %s", exc)

    return sent


def notify_async(title, body="", level="info", key=None, throttle=None):
    """Fire and forget, so a slow relay cannot stall the trading loop."""
    if not enabled():
        return
    t = threading.Thread(
        target=notify, args=(title, body, level, key, throttle), daemon=True)
    t.start()
