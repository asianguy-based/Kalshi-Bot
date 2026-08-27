import os
import sqlite3
import logging
import secrets

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, session)
from flask_login import (LoginManager, UserMixin, login_user, login_required,
                         logout_user)
from flask_bcrypt import Bcrypt
from cryptography.fernet import Fernet, InvalidToken

import auth
from bot_engine import bot_instance, log_buffer
from risk import risk_manager

logger = logging.getLogger("KalshiBot")

app = Flask(__name__)

# --- Hard configuration checks -------------------------------------------
# A trading app that silently boots with a dev key is a trading app that
# silently loses money. Fail loudly at startup instead.

SECRET_KEY = os.environ.get("SECRET_KEY")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
REQUIRE_HTTPS = os.environ.get("REQUIRE_HTTPS", "true").lower() != "false"
TRUST_PROXY = os.environ.get("TRUST_PROXY", "true").lower() != "false"

_missing = [name for name, val in (
    ("SECRET_KEY", SECRET_KEY),
    ("ENCRYPTION_KEY", ENCRYPTION_KEY),
    ("ACCESS_PASSPHRASE_HASH_B64 (or ACCESS_PASSPHRASE_HASH)",
     os.environ.get("ACCESS_PASSPHRASE_HASH_B64")
     or os.environ.get("ACCESS_PASSPHRASE_HASH")),
) if not val]
if _missing:
    raise RuntimeError(
        "Refusing to start. Missing required environment variables: "
        + ", ".join(_missing)
        + ". Run scripts/bootstrap_secrets.py to generate them."
    )

app.secret_key = SECRET_KEY
cipher = Fernet(ENCRYPTION_KEY.encode())

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=REQUIRE_HTTPS,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
    MAX_CONTENT_LENGTH=256 * 1024,
)

bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.session_protection = "strong"

DB_NAME = os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")

# Only these keys may be written through the config API, and only these are
# treated as secrets.
CONFIG_KEYS = (
    "kalshi_key_id", "kalshi_private_key",
    "market_keywords", "event_allowlist",
    "min_profit_pct", "max_bet_amount", "min_liquidity",
    "max_exposure", "daily_loss_limit", "max_orders_per_day",
    "poll_interval", "dry_run_mode",
)
SECRET_CONFIG_KEYS = ("kalshi_key_id", "kalshi_private_key")

NUMERIC_KEYS = {
    "min_profit_pct": (0.0, 100.0),
    "max_bet_amount": (0.0, 1_000_000.0),
    "min_liquidity": (0.0, 1_000_000.0),
    "max_exposure": (0.0, 1_000_000.0),
    "daily_loss_limit": (0.0, 1_000_000.0),
    "max_orders_per_day": (0.0, 100_000.0),
    "poll_interval": (2.0, 3600.0),
}


def db():
    os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config "
              "(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS trades ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT,"
              "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
              "kind TEXT, event_ticker TEXT, legs TEXT,"
              "contracts INTEGER, cost_cents INTEGER, fees_cents INTEGER,"
              "expected_profit_cents INTEGER, dry_run INTEGER,"
              "status TEXT, response TEXT)")
    # Sensible, conservative defaults on first boot.
    defaults = {
        "dry_run_mode": "true",
        "min_profit_pct": "1.5",
        "max_bet_amount": "25",
        "max_exposure": "100",
        "daily_loss_limit": "50",
        "max_orders_per_day": "20",
        "min_liquidity": "10",
        "poll_interval": "15",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_NAME)


# --- Single-operator session --------------------------------------------

class Operator(UserMixin):
    """There is exactly one operator. The id is a per-deployment constant."""
    id = "operator"


@login_manager.user_loader
def load_user(user_id):
    return Operator() if user_id == "operator" else None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    return redirect(url_for("login"))


@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if REQUIRE_HTTPS:
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    return resp


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def csrf_protect():
    """State-changing API calls must carry the session's CSRF token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.path == "/":          # login handles its own throttling
        return None
    sent = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not expected or not auth.constant_time_eq(sent, expected):
        return jsonify({"status": "error", "message": "csrf token invalid"}), 403
    return None


# --- Routes -------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def login():
    from flask_login import current_user
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        ip = auth.client_ip(request, trust_proxy=TRUST_PROXY)
        wait = auth.retry_after(ip)
        if wait > 0:
            return jsonify({
                "status": "error",
                "message": f"Too many attempts. Wait {wait}s.",
            }), 429

        data = request.get_json(silent=True) or {}
        if auth.check_passphrase(bcrypt, data.get("passphrase")):
            auth.record_success(ip)
            session.permanent = True
            login_user(Operator(), remember=False)
            _csrf_token()
            logger.info("Operator authenticated from %s", ip)
            return jsonify({"status": "success", "redirect": "/dashboard"})

        auth.record_failure(ip)
        return jsonify({"status": "error"}), 401

    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", csrf_token=_csrf_token())


@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def handle_config():
    conn = db()
    c = conn.cursor()
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            saved, errors = [], []
            for k in CONFIG_KEYS:
                if k not in data or data[k] in (None, ""):
                    continue
                val = str(data[k])

                if k in NUMERIC_KEYS:
                    lo, hi = NUMERIC_KEYS[k]
                    try:
                        num = float(val)
                    except ValueError:
                        errors.append(f"{k} must be a number")
                        continue
                    if not (lo <= num <= hi):
                        errors.append(f"{k} must be between {lo} and {hi}")
                        continue
                    val = str(num)

                if k == "dry_run_mode":
                    val = "true" if val.lower() in ("true", "1", "yes", "on") else "false"

                if k == "kalshi_private_key" and "BEGIN" not in val:
                    errors.append("kalshi_private_key must be a PEM private key")
                    continue

                if k in SECRET_CONFIG_KEYS:
                    val = cipher.encrypt(val.encode()).decode()

                c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                          (k, val))
                saved.append(k)
            conn.commit()

            if errors:
                return jsonify({"status": "error", "errors": errors,
                                "saved": saved}), 400
            bot_instance.invalidate_config()
            logger.info("Config updated: %s", ", ".join(saved) or "nothing")
            return jsonify({"status": "success", "saved": saved,
                            "message": "Configuration saved"})

        c.execute("SELECT key, value FROM config")
        out = {}
        for k, v in c.fetchall():
            out[k] = "********" if k in SECRET_CONFIG_KEYS else v
        out["credentials_set"] = all(
            k in out for k in SECRET_CONFIG_KEYS)
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/verify_credentials", methods=["POST"])
@login_required
def verify_credentials():
    ok, detail = bot_instance.verify_credentials()
    return jsonify({"status": "success" if ok else "error",
                    "detail": detail if ok else str(detail)}), (200 if ok else 400)


@app.route("/api/bot/logs")
@login_required
def get_logs():
    return jsonify({"logs": list(log_buffer.buffer)})


@app.route("/api/bot/status")
@login_required
def bot_status():
    return jsonify(bot_instance.status())


@app.route("/api/bot/opportunities")
@login_required
def opportunities():
    return jsonify({"opportunities": bot_instance.recent_opportunities()})


@app.route("/api/bot/start", methods=["POST"])
@login_required
def bot_start():
    return jsonify({"message": bot_instance.start_bot()})


@app.route("/api/bot/stop", methods=["POST"])
@login_required
def bot_stop():
    return jsonify({"message": bot_instance.stop_bot()})


@app.route("/api/bot/resume", methods=["POST"])
@login_required
def bot_resume():
    risk_manager.resume()
    return jsonify({"message": "Circuit breaker reset"})


@app.route("/api/bot/trade", methods=["POST"])
@login_required
def trigger_trade():
    data = request.get_json(silent=True) or {}
    opp_id = data.get("opportunity_id")
    if not opp_id:
        return jsonify({"status": "error",
                        "message": "opportunity_id required"}), 400
    result = bot_instance.execute_opportunity(opp_id)
    code = 200 if result.get("status") == "success" else 400
    return jsonify(result), code


@app.route("/api/trades")
@login_required
def trades():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, created_at, kind, event_ticker, legs, contracts, "
              "cost_cents, fees_cents, expected_profit_cents, dry_run, status "
              "FROM trades ORDER BY id DESC LIMIT 100")
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    return jsonify({"trades": rows, "risk": risk_manager.snapshot()})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
