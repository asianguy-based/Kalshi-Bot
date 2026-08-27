"""Tests for the parts where a bug costs real money."""

import unittest

from arbitrage import (trading_fee_cents, find_yes_no_spread,
                       find_event_basket, _best_ask)
from risk import RiskManager, TripWire


def book(yes_bids, no_bids):
    """Build a Kalshi-shaped orderbook. Levels are [price_cents, size]."""
    return {"orderbook": {"yes": yes_bids, "no": no_bids}}


class TestFees(unittest.TestCase):
    def test_fee_peaks_at_fifty_cents(self):
        mid = trading_fee_cents(100, 50)
        edge = trading_fee_cents(100, 5)
        self.assertGreater(mid, edge)

    def test_fee_is_rounded_up(self):
        # 0.07 * 1 * 0.5 * 0.5 = $0.0175 -> 1.75c -> 2c
        self.assertEqual(trading_fee_cents(1, 50), 2)

    def test_fee_zero_at_extremes(self):
        self.assertEqual(trading_fee_cents(100, 0), 0)
        self.assertEqual(trading_fee_cents(100, 100), 0)


class TestBestAsk(unittest.TestCase):
    def test_ask_is_derived_from_opposite_bid(self):
        # Best no bid of 40 means you can buy YES at 60.
        ask = _best_ask(book([[35, 10]], [[40, 20]]), "yes")
        self.assertEqual(ask, (60, 20))

    def test_ask_no_from_yes_bid(self):
        ask = _best_ask(book([[35, 10]], [[40, 20]]), "no")
        self.assertEqual(ask, (65, 10))

    def test_empty_book(self):
        self.assertIsNone(_best_ask(book([], []), "yes"))


class TestYesNoSpread(unittest.TestCase):
    def test_detects_real_arbitrage(self):
        # no bid 55 -> ask_yes 45; yes bid 50 -> ask_no 50. Total 95c.
        opp = find_yes_no_spread("MKT-A", book([[50, 100]], [[55, 100]]),
                                 min_profit_pct=1.0, min_liquidity=1,
                                 max_contracts=10)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.contracts, 10)
        self.assertEqual(opp.payout_cents, 1000)
        self.assertEqual(opp.cost_cents, 950)
        self.assertGreater(opp.profit_cents, 0)

    def test_rejects_when_fees_eat_the_edge(self):
        # Total ask 99c: 1c gross per pair, but fees near 50c are ~4c.
        opp = find_yes_no_spread("MKT-B", book([[50, 100]], [[51, 100]]),
                                 min_profit_pct=0.01, min_liquidity=1,
                                 max_contracts=10)
        self.assertIsNone(opp, "must not report a trade that loses after fees")

    def test_rejects_negative_edge(self):
        opp = find_yes_no_spread("MKT-C", book([[40, 100]], [[40, 100]]),
                                 min_profit_pct=0.0, min_liquidity=1)
        self.assertIsNone(opp)

    def test_respects_min_liquidity(self):
        opp = find_yes_no_spread("MKT-D", book([[50, 2]], [[55, 2]]),
                                 min_profit_pct=1.0, min_liquidity=50,
                                 max_contracts=100)
        self.assertIsNone(opp)

    def test_size_is_capped_by_thinnest_leg(self):
        opp = find_yes_no_spread("MKT-E", book([[50, 7]], [[55, 100]]),
                                 min_profit_pct=1.0, min_liquidity=1,
                                 max_contracts=1000)
        self.assertEqual(opp.contracts, 7)

    def test_respects_min_profit_pct(self):
        opp = find_yes_no_spread("MKT-F", book([[50, 100]], [[55, 100]]),
                                 min_profit_pct=99.0, min_liquidity=1,
                                 max_contracts=10)
        self.assertIsNone(opp)


class TestEventBasket(unittest.TestCase):
    def test_detects_underpriced_basket(self):
        # Three outcomes, each buyable at 30c YES -> 90c for a 100c payout.
        books = [(f"T{i}", book([[10, 100]], [[70, 100]])) for i in range(3)]
        opp = find_event_basket("EVT", books, min_profit_pct=1.0,
                                min_liquidity=1, max_contracts=10)
        self.assertIsNotNone(opp)
        self.assertEqual(len(opp.legs), 3)
        self.assertEqual(opp.cost_cents, 900)
        self.assertEqual(opp.payout_cents, 1000)

    def test_rejects_fairly_priced_basket(self):
        books = [(f"T{i}", book([[10, 100]], [[66, 100]])) for i in range(3)]
        opp = find_event_basket("EVT", books, min_profit_pct=1.0,
                                min_liquidity=1, max_contracts=10)
        self.assertIsNone(opp)

    def test_single_leg_is_not_a_basket(self):
        books = [("T0", book([[10, 100]], [[70, 100]]))]
        self.assertIsNone(find_event_basket("EVT", books))

    def test_missing_book_aborts(self):
        books = [("T0", book([[10, 100]], [[70, 100]])), ("T1", book([], []))]
        self.assertIsNone(find_event_basket("EVT", books))


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.cfg = {"max_bet_amount": "25", "max_exposure": "100",
                    "daily_loss_limit": "50", "max_orders_per_day": "5"}

    def test_allows_trade_within_limits(self):
        self.assertTrue(self.rm.check_trade(1000, self.cfg))

    def test_blocks_oversized_trade(self):
        with self.assertRaises(TripWire):
            self.rm.check_trade(5000, self.cfg)     # $50 > $25 max bet

    def test_blocks_exposure_breach(self):
        for _ in range(5):
            self.rm.record_order(2000)              # $100 open
        with self.assertRaises(TripWire):
            self.rm.check_trade(500, self.cfg)

    def test_blocks_after_daily_loss_limit(self):
        self.rm.record_settlement(-5000, 0)         # -$50
        with self.assertRaises(TripWire):
            self.rm.check_trade(100, self.cfg)
        self.assertTrue(self.rm.halted)

    def test_blocks_after_order_cap(self):
        for _ in range(5):
            self.rm.record_order(10)
        with self.assertRaises(TripWire):
            self.rm.check_trade(10, self.cfg)

    def test_halted_bot_refuses_everything(self):
        self.rm.halt("test")
        with self.assertRaises(TripWire):
            self.rm.check_trade(1, self.cfg)
        self.rm.resume()
        self.assertTrue(self.rm.check_trade(1, self.cfg))

    def test_consecutive_errors_trip_breaker(self):
        for _ in range(5):
            self.rm.note_error(RuntimeError("boom"), threshold=5)
        self.assertTrue(self.rm.halted)

    def test_success_resets_error_count(self):
        self.rm.note_error(RuntimeError("x"), threshold=5)
        self.rm.note_success()
        self.assertEqual(self.rm.consecutive_errors, 0)

    def test_win_loss_tracking(self):
        self.rm.record_settlement(500, 0)
        self.rm.record_settlement(-200, 0)
        snap = self.rm.snapshot()
        self.assertEqual(snap["wins"], 1)
        self.assertEqual(snap["losses"], 1)
        self.assertEqual(snap["realized_pnl"], 3.0)


class TestAuthThrottle(unittest.TestCase):
    def test_backoff_after_free_attempts(self):
        import auth
        auth._ATTEMPTS.clear()
        ip = "203.0.113.9"
        self.assertEqual(auth.retry_after(ip), 0)
        for _ in range(5):
            auth.record_failure(ip)
        self.assertGreater(auth.retry_after(ip), 0)
        auth.record_success(ip)
        self.assertEqual(auth.retry_after(ip), 0)

    def test_no_passphrase_configured_denies(self):
        import auth, os
        old = os.environ.pop("ACCESS_PASSPHRASE_HASH", None)
        try:
            self.assertFalse(auth.is_configured())
            self.assertFalse(auth.check_passphrase(None, "anything"))
        finally:
            if old:
                os.environ["ACCESS_PASSPHRASE_HASH"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
