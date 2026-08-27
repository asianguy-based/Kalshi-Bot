"""Risk limits and circuit breakers.

A bot that can place orders must be able to stop placing orders. These are the
guards that stand between a logic bug and an empty account.
"""

import time
import threading
import logging

logger = logging.getLogger("KalshiBot")


class TripWire(Exception):
    """Raised when a hard limit refuses a trade."""


class RiskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset_day()
        self.halted = False
        self.halt_reason = ""
        self.consecutive_errors = 0

    # --- daily bookkeeping ------------------------------------------------

    def reset_day(self):
        self._day = time.strftime("%Y-%m-%d")
        self.open_exposure_cents = 0
        self.realized_pnl_cents = 0
        self.orders_today = 0
        self.wins = 0
        self.losses = 0

    def _roll_day_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            logger.info("New trading day (%s). Resetting daily counters.", today)
            self.reset_day()

    # --- circuit breaker --------------------------------------------------

    def halt(self, reason):
        with self._lock:
            self.halted = True
            self.halt_reason = reason
        logger.error("CIRCUIT BREAKER TRIPPED: %s", reason)

    def resume(self):
        with self._lock:
            self.halted = False
            self.halt_reason = ""
            self.consecutive_errors = 0
        logger.info("Circuit breaker manually reset.")

    def note_error(self, exc, threshold=5):
        with self._lock:
            self.consecutive_errors += 1
            count = self.consecutive_errors
        logger.error("Engine error %d/%d: %s", count, threshold, exc)
        if count >= threshold:
            self.halt(f"{count} consecutive errors; last: {exc}")

    def note_success(self):
        with self._lock:
            self.consecutive_errors = 0

    # --- pre-trade gate ---------------------------------------------------

    def check_trade(self, cost_cents, config):
        """Raise TripWire if this trade must not happen."""
        self._roll_day_if_needed()

        if self.halted:
            raise TripWire(f"Bot halted: {self.halt_reason}")

        max_bet = int(float(config.get("max_bet_amount", 0)) * 100)
        if max_bet > 0 and cost_cents > max_bet:
            raise TripWire(f"Trade cost ${cost_cents/100:.2f} exceeds "
                           f"max bet ${max_bet/100:.2f}")

        max_exposure = int(float(config.get("max_exposure", 0)) * 100)
        if max_exposure > 0 and self.open_exposure_cents + cost_cents > max_exposure:
            raise TripWire(f"Would breach max exposure "
                           f"${max_exposure/100:.2f} "
                           f"(open ${self.open_exposure_cents/100:.2f})")

        daily_loss_limit = int(float(config.get("daily_loss_limit", 0)) * 100)
        if daily_loss_limit > 0 and self.realized_pnl_cents <= -daily_loss_limit:
            self.halt(f"Daily loss limit ${daily_loss_limit/100:.2f} reached")
            raise TripWire("Daily loss limit reached")

        max_orders = int(config.get("max_orders_per_day", 0) or 0)
        if max_orders > 0 and self.orders_today >= max_orders:
            raise TripWire(f"Daily order cap {max_orders} reached")

        return True

    # --- post-trade bookkeeping ------------------------------------------

    def record_order(self, cost_cents):
        with self._lock:
            self.orders_today += 1
            self.open_exposure_cents += cost_cents

    def record_settlement(self, pnl_cents, cost_cents):
        with self._lock:
            self.realized_pnl_cents += pnl_cents
            self.open_exposure_cents = max(0, self.open_exposure_cents - cost_cents)
            if pnl_cents > 0:
                self.wins += 1
            elif pnl_cents < 0:
                self.losses += 1

    def snapshot(self):
        self._roll_day_if_needed()
        total = self.wins + self.losses
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "open_exposure": round(self.open_exposure_cents / 100, 2),
            "realized_pnl": round(self.realized_pnl_cents / 100, 2),
            "orders_today": self.orders_today,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(100.0 * self.wins / total, 1) if total else None,
        }


risk_manager = RiskManager()
