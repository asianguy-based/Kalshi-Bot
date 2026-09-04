"""Once-a-day summary of what the engine did, pushed to the operator.

The point of a multi-day dry run is the data it produces. Without a digest,
reading that data means logging into a dashboard every day and remembering
to do it. This turns the run into something that reports to you instead.

Deliberately simple: one thread, one query, one message per day. It reads
the trades table directly rather than going through the engine, so a wedged
engine still produces a report - which is itself the signal you want.
"""

import os
import time
import sqlite3
import logging
import threading

from notify import notify, enabled as notify_enabled

logger = logging.getLogger("KalshiBot")

DB_NAME = os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")
# Hour of day (server local time, UTC on a default droplet) to send.
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "13") or 13)
ENABLED = os.environ.get("DIGEST_ENABLED", "true").lower() == "true"


def _fetch(sql, args=()):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def build_digest():
    """Return (title, body). Pure read - safe to call any time."""
    rows = _fetch(
        "SELECT status, dry_run, COUNT(*), "
        "COALESCE(SUM(expected_profit_cents),0), COALESCE(SUM(fees_cents),0), "
        "COALESCE(SUM(cost_cents),0) "
        "FROM trades WHERE created_at >= datetime('now','-1 day') "
        "GROUP BY status, dry_run")

    # Only statuses that represent a trade the bot would actually have taken
    # count toward profit. A 'refused' row was blocked by a risk breaker and
    # a 'partial' one never completed - including them would flatter the
    # results, which is the opposite of what this run is for.
    COUNTED = ("dry_run", "filled")
    total = _fetch(
        "SELECT COUNT(*), COALESCE(SUM(expected_profit_cents),0) "
        "FROM trades WHERE dry_run=1 AND status IN (?,?)", COUNTED)

    if not rows:
        body = ("No opportunities recorded in the last 24 hours.\n\n"
                "That is a real result, not a malfunction: it means nothing "
                "cleared the minimum profit threshold after fees. If it "
                "persists for the whole run, the honest conclusion is that "
                "the edge is not there at this size.")
        return "Daily digest - no activity", body

    lines, day_count, day_profit, day_fees = [], 0, 0, 0
    skipped = 0
    for status, dry, count, profit, fees, cost in rows:
        mode = "DRY" if dry else "LIVE"
        counted = status in COUNTED
        note = "" if counted else "   (not counted)"
        lines.append(
            f"  {mode:4} {status:10} x{count:<4} "
            f"expected +${profit/100:,.2f}  fees ${fees/100:,.2f}{note}")
        day_count += count
        if counted:
            day_profit += profit
            day_fees += fees
        else:
            skipped += count

    run_count, run_profit = (total[0] if total else (0, 0))

    body = (
        f"Last 24 hours: {day_count} recorded opportunities\n"
        + "\n".join(lines)
        + f"\n\nExpected profit (24h): ${day_profit/100:,.2f}"
        + f"\nFees modelled (24h):   ${day_fees/100:,.2f}"
        + (f"\nExcluded {skipped} refused/partial row(s) from the totals."
           if skipped else "")
        + f"\n\nRun total (dry): {run_count} opportunities, "
          f"${run_profit/100:,.2f} expected profit"
        + "\n\nReminder: dry-run figures are a CEILING. They assume every "
          "leg would have filled at the quoted price, which real "
          "competition does not guarantee."
        + "\n\n-- The record is kept.")
    return f"Daily digest - {day_count} opportunities", body


class DailyDigest:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if not (ENABLED and notify_enabled()):
            logger.info("Daily digest not enabled (no notification transport).")
            return False
        if self._thread and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Daily digest scheduled for hour %02d.", DIGEST_HOUR)
        return True

    def _loop(self):
        # Track the last date sent so a restart inside the target hour does
        # not re-send, and a missed hour is not retried until tomorrow.
        last_sent = None
        while not self._stop.is_set():
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if now.tm_hour == DIGEST_HOUR and last_sent != today:
                try:
                    title, body = build_digest()
                    ok = notify(title, body, level="info",
                                key=f"digest-{today}", throttle=0)
                    # Log the outcome either way. A silent success is
                    # indistinguishable from a digest that never ran, which
                    # is the exact ambiguity this feature exists to remove.
                    logger.info("Daily digest %s: %s",
                                "sent" if ok else "FAILED TO SEND", title)
                    last_sent = today
                except Exception as exc:
                    logger.error("Digest failed: %s", exc)
                    last_sent = today          # do not hammer on failure
            self._stop.wait(300)

    def stop(self):
        self._stop.set()


daily_digest = DailyDigest()
