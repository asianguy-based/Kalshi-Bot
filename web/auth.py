"""Invite-only authentication: one shared passphrase, no usernames, no signup.

Design notes
------------
* There is exactly one credential: ACCESS_PASSPHRASE_HASH (a bcrypt hash held in
  the environment, never in the repo, never in the database).
* There is no user table, no registration route, and no password-reset route.
  Rotating access means rotating one environment variable and restarting.
* Failed attempts are rate limited per client IP with exponential backoff, so a
  single password box is not an open brute-force target.
"""

import base64
import os
import time
import threading
import hmac
import logging
from collections import defaultdict

logger = logging.getLogger("KalshiBot")

# --- Brute-force throttle -------------------------------------------------

_LOCK = threading.Lock()
_ATTEMPTS = defaultdict(list)          # ip -> [timestamps of failures]
_WINDOW_SECONDS = 15 * 60
_FREE_ATTEMPTS = 5                     # failures before backoff begins
_MAX_BACKOFF = 15 * 60                 # cap the lockout at 15 minutes


def _prune(ip, now):
    _ATTEMPTS[ip] = [t for t in _ATTEMPTS[ip] if now - t < _WINDOW_SECONDS]
    if not _ATTEMPTS[ip]:
        _ATTEMPTS.pop(ip, None)


def retry_after(ip):
    """Seconds the caller must wait, or 0 if an attempt is allowed now."""
    now = time.time()
    with _LOCK:
        _prune(ip, now)
        failures = len(_ATTEMPTS.get(ip, ()))
        if failures < _FREE_ATTEMPTS:
            return 0
        # 2s, 4s, 8s, 16s ... capped
        penalty = min(2 ** (failures - _FREE_ATTEMPTS + 1), _MAX_BACKOFF)
        elapsed = now - _ATTEMPTS[ip][-1]
        return max(0, int(penalty - elapsed))


def record_failure(ip):
    with _LOCK:
        _ATTEMPTS[ip].append(time.time())
        count = len(_ATTEMPTS[ip])
    logger.warning("Failed login attempt from %s (%d in window)", ip, count)


def record_success(ip):
    with _LOCK:
        _ATTEMPTS.pop(ip, None)


# --- Passphrase check ----------------------------------------------------

def _passphrase_hash():
    """The configured bcrypt hash, or "" if unset.

    Prefers ACCESS_PASSPHRASE_HASH_B64 (base64 of the bcrypt hash) because a
    raw bcrypt hash contains "$" and gets mangled by shell expansion when an
    operator sources the env file by hand. The raw variable is still accepted
    for compatibility.
    """
    b64 = os.environ.get("ACCESS_PASSPHRASE_HASH_B64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64).decode()
        except Exception as exc:
            logger.error("ACCESS_PASSPHRASE_HASH_B64 is not valid base64: %s", exc)
            return ""
    return os.environ.get("ACCESS_PASSPHRASE_HASH", "").strip()


def is_configured():
    return bool(_passphrase_hash())


def check_passphrase(bcrypt_ext, candidate):
    """Constant-time-ish verification of the submitted passphrase.

    Returns False when no passphrase is configured: a misconfigured deployment
    must be closed, never open.
    """
    stored = _passphrase_hash()
    if not stored:
        logger.error("ACCESS_PASSPHRASE_HASH is not set - refusing all logins.")
        return False
    if not candidate or not isinstance(candidate, str):
        return False
    # bcrypt truncates at 72 bytes; reject absurd input before hashing.
    if len(candidate.encode("utf-8")) > 256:
        return False
    try:
        return bool(bcrypt_ext.check_password_hash(stored, candidate))
    except Exception as exc:                       # malformed hash, etc.
        logger.error("Passphrase check failed: %s", exc)
        return False


def constant_time_eq(a, b):
    return hmac.compare_digest(str(a), str(b))


def client_ip(request, trust_proxy=True):
    """Real client IP. Only trusts X-Forwarded-For when explicitly enabled,
    because a spoofable header would let an attacker reset the throttle."""
    if trust_proxy:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"
