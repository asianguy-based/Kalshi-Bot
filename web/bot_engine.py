import os
import time
import json
import uuid
import sqlite3
import logging
import threading
from collections import deque

from cryptography.fernet import Fernet, InvalidToken

from kalshi_client import KalshiClient, KalshiAuthError
from arbitrage import find_yes_no_spread, find_event_basket
from risk import risk_manager, TripWire
from notify import notify_async, enabled as notify_enabled


# --- Log buffer ---------------------------------------------------------

class LogBufferHandler(logging.Handler):
    def __init__(self, capacity=500):
        super().__init__()
        self.buffer = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = LogBufferHandler()
logger = logging.getLogger("KalshiBot")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(log_buffer)
logger.addHandler(logging.StreamHandler())
logger.propagate = False

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None
DB_NAME = os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")

SECRET_CONFIG_KEYS = ("kalshi_key_id", "kalshi_private_key")


def _to_float(v):
    """Kalshi returns the *_dollars and *_fp fields as STRINGS ('0.0400')."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def market_bids_cents(m):
    """Best resting YES and NO bid, in whole cents.

    Kalshi migrated the market payload to dollar-denominated string fields
    (yes_bid_dollars) and now returns the legacy integer-cent fields
    (yes_bid) as null. Reading only the legacy names makes every market look
    unquoted, so the pre-filter selects nothing and the engine reports
    "0 markets worth an orderbook lookup" forever - a silent no-op rather
    than an error. Prefer the new fields, fall back to the old ones so this
    keeps working whichever shape the API serves.
    """
    yb = m.get("yes_bid")
    nb = m.get("no_bid")
    if yb is None or nb is None:
        yb = round(_to_float(m.get("yes_bid_dollars")) * 100)
        nb = round(_to_float(m.get("no_bid_dollars")) * 100)
    return int(yb or 0), int(nb or 0)


class BotManager:
    def __init__(self):
        self.is_running = False
        self.thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._config = {}
        self._config_stale = True
        self._client = None
        self._opportunities = {}      # id -> Opportunity
        self._opp_order = deque(maxlen=50)
        self.last_scan_at = None
        self.markets_seen = 0
        self._consecutive_scan_errors = 0
        self.started_at = None

    # --- config ---------------------------------------------------------

    def invalidate_config(self):
        with self._lock:
            self._config_stale = True
            self._client = None

    def config(self):
        with self._lock:
            if not self._config_stale:
                return dict(self._config)
        cfg = self._load_config()
        with self._lock:
            self._config = cfg
            self._config_stale = False
        return dict(cfg)

    def _load_config(self):
        cfg = {}
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            c = conn.cursor()
            c.execute("SELECT key, value FROM config")
            rows = c.fetchall()
            conn.close()
        except Exception as exc:
            logger.error("Config read failed: %s", exc)
            return cfg

        for k, v in rows:
            if k in SECRET_CONFIG_KEYS:
                if not cipher:
                    continue
                try:
                    cfg[k] = cipher.decrypt(v.encode()).decode()
                except InvalidToken:
                    logger.error("Could not decrypt %s - ENCRYPTION_KEY may have "
                                 "changed. Re-enter credentials.", k)
                except Exception as exc:
                    logger.error("Decrypt error for %s: %s", k, exc)
            else:
                cfg[k] = v
        return cfg

    def client(self):
        with self._lock:
            if self._client is not None:
                return self._client
        cfg = self.config()
        key_id = cfg.get("kalshi_key_id")
        pem = cfg.get("kalshi_private_key")
        if not (key_id and pem):
            return None
        try:
            client = KalshiClient(key_id=key_id, private_key_pem=pem)
        except KalshiAuthError as exc:
            logger.error("Credential problem: %s", exc)
            return None
        with self._lock:
            self._client = client
        return client

    def verify_credentials(self):
        client = self.client()
        if not client:
            return False, "Credentials not configured"
        return client.verify_credentials()

    # --- lifecycle ------------------------------------------------------

    def start_bot(self):
        with self._lock:
            if self.is_running:
                return "Already running"
            if risk_manager.halted:
                return f"Refusing to start: {risk_manager.halt_reason}"
            self.is_running = True
            self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._consecutive_scan_errors = 0
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._set_desired_state("running")
        return "Started"

    # --- unattended operation -------------------------------------------
    #
    # A multi-day run must survive a container restart. Without this, the
    # process comes back up serving a dashboard with the engine IDLE, and
    # the run silently ends at whatever hour Docker happened to restart -
    # which is exactly the failure you would not notice for days.

    def _set_desired_state(self, state):
        """Persist whether the operator wants the engine running."""
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            conn.execute(
                "INSERT INTO config (key, value) VALUES ('desired_state', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (state,))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("Could not persist desired_state: %s", exc)

    def _desired_state(self):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            row = conn.execute(
                "SELECT value FROM config WHERE key='desired_state'").fetchone()
            conn.close()
            return row[0] if row else "stopped"
        except Exception as exc:
            logger.error("Could not read desired_state: %s", exc)
            return "stopped"

    def resume_if_desired(self):
        """Called once at process start. Restarts the engine if the operator
        had it running when the process died, and says so out loud."""
        if self._desired_state() != "running":
            return False
        if risk_manager.halted:
            logger.error("Not auto-resuming: breaker is tripped.")
            return False
        cfg = self.config()
        if not (cfg.get("kalshi_key_id") and cfg.get("kalshi_private_key")):
            logger.warning("Auto-resume wanted, but no credentials set.")
            return False
        logger.info("Auto-resuming engine after restart.")
        msg = self.start_bot()
        notify_async(
            "Engine auto-resumed after restart",
            "The process restarted and the engine was brought back up "
            "automatically, so the run continues uninterrupted.",
            level="warning", key="auto-resume")
        return msg == "Started"

    def stop_bot(self):
        with self._lock:
            if not self.is_running:
                self._set_desired_state("stopped")
                return "Already stopped"
            self.is_running = False
        # Recorded before joining: an operator stop must not be undone by a
        # restart that happens while the thread is still winding down.
        self._set_desired_state("stopped")
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        logger.info("Bot stopped by operator.")
        return "Stopped"

    def status(self):
        cfg = self.config()
        return {
            "state": "RUNNING" if self.is_running else "IDLE",
            "dry_run": cfg.get("dry_run_mode", "true") == "true",
            "credentials_set": bool(cfg.get("kalshi_key_id")
                                    and cfg.get("kalshi_private_key")),
            "started_at": self.started_at,
            "notifications": notify_enabled(),
            "last_scan_at": self.last_scan_at,
            "markets_seen": self.markets_seen,
            "open_opportunities": len(self._opportunities),
            "risk": risk_manager.snapshot(),
        }

    def recent_opportunities(self):
        out = []
        for opp_id in list(self._opp_order)[::-1]:
            opp = self._opportunities.get(opp_id)
            if not opp:
                continue
            out.append({
                "id": opp_id,
                "kind": opp.kind,
                "event_ticker": opp.event_ticker,
                "contracts": opp.contracts,
                "cost": round(opp.cost_cents / 100, 2),
                "fees": round(opp.fees_cents / 100, 2),
                "profit": round(opp.profit_cents / 100, 2),
                "profit_pct": round(opp.profit_pct, 2),
                "detail": opp.detail,
                "legs": [{"ticker": l.ticker, "side": l.side,
                          "price_cents": l.price_cents} for l in opp.legs],
                "summary": opp.summary(),
            })
        return out

    # --- scanning -------------------------------------------------------

    def _fetch_markets(self, client, keywords, max_pages=5):
        """Collect tradeable markets.

        Paginated via /events with nested markets, NOT via /markets. The flat
        /markets feed is now dominated by auto-generated KXMVECROSSCATEGORY
        combination shards - 12,000+ of them, none with any 24h volume - so
        the first N pages contain nothing tradeable and the real markets are
        never reached. Enumerating events yields the actual contracts
        (~1,600 events, ~1,300 quoted markets) in the same number of calls.
        """
        markets, cursor, seen = [], None, set()
        for _ in range(max_pages):
            params = {"status": "open", "limit": 200,
                      "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = client.get_events(**params)
            except Exception as exc:
                logger.error("Event fetch failed: %s", exc)
                break
            batch = data.get("events", [])
            for ev in batch:
                ev_ticker = ev.get("event_ticker", "")
                # Skip the synthetic cross-category baskets outright: they
                # are unquoted and would waste the orderbook budget.
                if "MVECROSS" in ev_ticker:
                    continue
                for m in (ev.get("markets") or []):
                    t = m.get("ticker")
                    if not t or t in seen:
                        continue
                    seen.add(t)
                    m.setdefault("event_ticker", ev_ticker)
                    m.setdefault("title", ev.get("title", ""))
                    markets.append(m)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        if keywords:
            kws = [k.strip().lower() for k in keywords.split(",") if k.strip()]
            if kws:
                markets = [
                    m for m in markets
                    if any(k in " ".join(str(m.get(f, "")) for f in
                                         ("title", "ticker", "event_ticker",
                                          "subtitle", "category")).lower()
                           for k in kws)
                ]
        return markets

    def _register(self, opp):
        opp_id = uuid.uuid4().hex[:12]
        self._opportunities[opp_id] = opp
        self._opp_order.append(opp_id)
        # Bound memory: drop anything that fell out of the deque.
        live = set(self._opp_order)
        for stale in [k for k in self._opportunities if k not in live]:
            self._opportunities.pop(stale, None)
        return opp_id

    def _scan_once(self, client, cfg):
        min_profit = float(cfg.get("min_profit_pct", 1.5))
        min_liq = int(float(cfg.get("min_liquidity", 10)))
        max_contracts = max(1, int(float(cfg.get("max_bet_amount", 25))))

        markets = self._fetch_markets(client, cfg.get("market_keywords", ""))
        self.markets_seen = len(markets)
        self.last_scan_at = time.strftime("%Y-%m-%d %H:%M:%S")

        if not markets:
            logger.info("No markets matched. Check keywords.")
            return 0

        logger.info("Scanning %d markets for spreads.", len(markets))
        found = 0

        # 1. Single-market YES/NO cross-spread.
        # Only pull orderbooks for markets whose quoted spread is even close,
        # so we do not hammer the API with hopeless lookups.
        candidates = []
        for m in markets:
            yb, nb = market_bids_cents(m)
            if yb and nb and (100 - yb) + (100 - nb) < 104:
                candidates.append(m)

        # Only the first 60 get an orderbook lookup, so order matters: put the
        # tightest quoted spreads first, and break ties toward the market with
        # real 24h volume. Sorting by cost alone floods the budget with
        # zero-volume long shots that quote 100c but cannot actually be filled.
        candidates.sort(key=lambda m: (
            sum(100 - b for b in market_bids_cents(m)),
            -_to_float(m.get("volume_24h_fp") or m.get("volume") or 0)))

        logger.info("%d markets worth an orderbook lookup.", len(candidates))
        for m in candidates[:60]:
            if self._stop_event.is_set():
                break
            ticker = m.get("ticker")
            try:
                book = client.get_orderbook(ticker)
            except Exception as exc:
                logger.debug("Orderbook %s failed: %s", ticker, exc)
                continue
            opp = find_yes_no_spread(
                ticker, book, min_profit_pct=min_profit,
                min_liquidity=min_liq, max_contracts=max_contracts,
                event_ticker=m.get("event_ticker", ""))
            if opp:
                opp_id = self._register(opp)
                found += 1
                logger.info("OPPORTUNITY %s %s", opp_id, opp.summary())
            time.sleep(0.15)          # be polite to the API

        # 2. Event baskets, but ONLY for events the operator explicitly
        # allowlisted as mutually exclusive AND collectively exhaustive.
        # Guessing this from the API is how you buy a "basket" that isn't one.
        allowlist = [e.strip() for e in
                     (cfg.get("event_allowlist", "") or "").split(",") if e.strip()]
        for event in allowlist:
            if self._stop_event.is_set():
                break
            legs = [m for m in markets if m.get("event_ticker") == event]
            if len(legs) < 2:
                continue
            books = []
            ok = True
            for m in legs:
                try:
                    books.append((m["ticker"], client.get_orderbook(m["ticker"])))
                except Exception as exc:
                    logger.debug("Basket leg %s failed: %s", m.get("ticker"), exc)
                    ok = False
                    break
                time.sleep(0.15)
            if not ok:
                continue
            opp = find_event_basket(event, books, min_profit_pct=min_profit,
                                    min_liquidity=min_liq,
                                    max_contracts=max_contracts)
            if opp:
                opp_id = self._register(opp)
                found += 1
                logger.info("OPPORTUNITY %s %s", opp_id, opp.summary())

        if not found:
            logger.info("No profitable spreads after fees.")
        return found

    def _run_loop(self):
        logger.info("Engine started.")
        while not self._stop_event.is_set():
            cfg = self.config()
            interval = max(5.0, float(cfg.get("poll_interval", 15)))
            try:
                if risk_manager.halted:
                    logger.error("Halted: %s. Stopping engine.",
                                 risk_manager.halt_reason)
                    # An unattended halt is the single most important thing
                    # to escalate: the bot has stopped trading and will not
                    # restart itself. No throttle - this fires once, because
                    # the loop exits immediately after.
                    notify_async(
                        "CIRCUIT BREAKER TRIPPED - bot stopped",
                        f"Reason: {risk_manager.halt_reason}\n\n"
                        "The engine has exited and will NOT resume on its "
                        "own. Reset the breaker from the dashboard after "
                        "checking your Kalshi positions.",
                        level="critical", key="halt", throttle=0)
                    break
                client = self.client()
                if not client:
                    logger.error("Credentials not configured; engine idling.")
                    notify_async(
                        "Idling - no credentials configured",
                        "The engine is running but has no Kalshi API "
                        "credentials, so it cannot scan. Add them in the "
                        "dashboard.",
                        level="warning", key="no-credentials")
                    self._stop_event.wait(30)
                    continue
                self._scan_once(client, cfg)
                risk_manager.note_success()
                self._consecutive_scan_errors = 0
            except Exception as exc:
                risk_manager.note_error(exc)
                self._consecutive_scan_errors += 1
                # Escalate a persistent scan failure. One transient API blip
                # is noise; a sustained outage means the run is producing no
                # data and the operator should know before day seven.
                if self._consecutive_scan_errors in (3, 25):
                    notify_async(
                        "Repeated scan failures",
                        f"{self._consecutive_scan_errors} consecutive scan "
                        f"errors. Most recent: {exc}",
                        level="warning", key="scan-errors")
            self._stop_event.wait(interval)

        with self._lock:
            self.is_running = False
        logger.info("Engine loop exited.")

    # --- execution ------------------------------------------------------

    def _record_trade(self, opp, dry_run, status, response):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            conn.execute(
                "INSERT INTO trades (kind, event_ticker, legs, contracts, "
                "cost_cents, fees_cents, expected_profit_cents, dry_run, "
                "status, response) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (opp.kind, opp.event_ticker,
                 json.dumps([{"ticker": l.ticker, "side": l.side,
                              "price_cents": l.price_cents} for l in opp.legs]),
                 opp.contracts, opp.cost_cents, opp.fees_cents,
                 opp.profit_cents, 1 if dry_run else 0,
                 status, json.dumps(response)[:4000]))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("Could not record trade: %s", exc)

    def execute_opportunity(self, opp_id):
        """Execute a previously detected opportunity, by id.

        Taking an id rather than a raw ticker is deliberate: the server decides
        what may be traded and at what size. A client that can name an arbitrary
        ticker is a client that can drain the account.
        """
        opp = self._opportunities.get(opp_id)
        if not opp:
            return {"status": "error",
                    "message": "Unknown or expired opportunity. Re-scan."}

        cfg = self.config()
        dry_run = cfg.get("dry_run_mode", "true") == "true"

        try:
            risk_manager.check_trade(opp.cost_cents, cfg)
        except TripWire as exc:
            logger.error("Trade refused: %s", exc)
            self._record_trade(opp, dry_run, "refused", {"reason": str(exc)})
            return {"status": "error", "message": str(exc)}

        client = self.client()
        if not client:
            return {"status": "error", "message": "Credentials not configured"}

        # Re-verify against a fresh orderbook. Prices move; a stale edge is a
        # loss dressed as a win.
        try:
            fresh = self._revalidate(client, opp, cfg)
        except Exception as exc:
            logger.error("Revalidation failed: %s", exc)
            return {"status": "error", "message": f"Revalidation failed: {exc}"}

        if not fresh:
            self._opportunities.pop(opp_id, None)
            msg = "Edge disappeared on re-check. No order placed."
            logger.info(msg)
            return {"status": "error", "message": msg}
        opp = fresh

        if dry_run:
            logger.info("[DRY RUN] Would execute: %s", opp.summary())
            self._record_trade(opp, True, "dry_run", {"summary": opp.summary()})
            return {"status": "success", "dry_run": True,
                    "message": "Dry run - no order sent", "detail": opp.summary()}

        # Live execution, leg by leg, fill-or-kill.
        results, placed = [], []
        for leg in opp.legs:
            client_order_id = f"{opp_id}-{leg.side}-{uuid.uuid4().hex[:8]}"
            try:
                resp = client.create_order(
                    ticker=leg.ticker, side=leg.side, action="buy",
                    count=opp.contracts, price_cents=leg.price_cents,
                    client_order_id=client_order_id,
                    order_type="limit", time_in_force="fill_or_kill")
                results.append(resp)
                placed.append(resp)
                logger.info("Filled leg %s %s x%d @ %dc",
                            leg.side, leg.ticker, opp.contracts, leg.price_cents)
            except Exception as exc:
                logger.error("Leg failed (%s %s): %s", leg.side, leg.ticker, exc)
                # A half-filled arbitrage is a naked position. Halt so a human
                # decides how to unwind it, rather than letting the loop
                # compound the mistake.
                risk_manager.halt(
                    f"Partial execution on {opp.event_ticker}: leg "
                    f"{leg.side}/{leg.ticker} failed after {len(placed)} "
                    f"leg(s) filled. Manual unwind required.")
                self._record_trade(opp, False, "partial",
                                   {"placed": placed, "error": str(exc)})
                return {"status": "error", "partial": True,
                        "message": ("Partial fill - bot halted. Check your "
                                    "Kalshi positions and unwind manually."),
                        "placed": len(placed)}

        risk_manager.record_order(opp.cost_cents)
        self._record_trade(opp, False, "filled", {"orders": results})
        self._opportunities.pop(opp_id, None)
        logger.info("Executed %s for expected +%dc", opp.event_ticker,
                    opp.profit_cents)
        return {"status": "success", "dry_run": False,
                "message": f"Executed {len(results)} legs",
                "expected_profit": round(opp.profit_cents / 100, 2)}

    def _revalidate(self, client, opp, cfg):
        min_profit = float(cfg.get("min_profit_pct", 1.5))
        min_liq = int(float(cfg.get("min_liquidity", 10)))
        max_contracts = max(1, int(float(cfg.get("max_bet_amount", 25))))

        if opp.kind == "yes_no_spread":
            ticker = opp.legs[0].ticker
            book = client.get_orderbook(ticker)
            return find_yes_no_spread(ticker, book, min_profit_pct=min_profit,
                                      min_liquidity=min_liq,
                                      max_contracts=max_contracts,
                                      event_ticker=opp.event_ticker)
        if opp.kind == "event_basket":
            books = [(l.ticker, client.get_orderbook(l.ticker)) for l in opp.legs]
            return find_event_basket(opp.event_ticker, books,
                                     min_profit_pct=min_profit,
                                     min_liquidity=min_liq,
                                     max_contracts=max_contracts)
        return None


bot_instance = BotManager()
