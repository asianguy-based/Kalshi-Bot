"""Arbitrage detection for Kalshi binary markets.

The honest edge on Kalshi is not "two markets disagree." It is the structural
mispricing that shows up inside a single event: a set of mutually exclusive,
collectively exhaustive outcomes whose prices do not sum to 100 cents.

Two shapes are worth trading:

1. YES/NO cross-spread on one market
   Buying YES at ask_yes and NO at ask_no locks a payout of 100c per contract
   pair. If ask_yes + ask_no < 100 - fees, that is a locked profit.

2. Mutually-exclusive event basket ("who wins the election")
   Exactly one outcome resolves YES. Buying NO on every outcome pays
   (n - 1) * 100c. Buying YES on every outcome pays 100c. So if the sum of YES
   asks across all outcomes is below 100 - fees, buy every YES leg.

Fees matter and are not optional. Kalshi charges a per-contract trading fee
that peaks near 50c prices; ignoring it turns most "arbitrage" into a loss.
Fee formula (Kalshi published schedule):

    fee = ceil(0.07 * C * P * (1 - P))   dollars, where C = contracts,
                                         P = price in dollars (0..1)

Everything here reports opportunities. Nothing here places an order.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("KalshiBot")

FEE_RATE = 0.07


def trading_fee_cents(contracts, price_cents):
    """Kalshi trading fee in CENTS, rounded up to the next cent as Kalshi does."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    fee_dollars = FEE_RATE * contracts * p * (1.0 - p)
    return int(math.ceil(fee_dollars * 100))


@dataclass
class Leg:
    ticker: str
    side: str            # "yes" | "no"
    price_cents: int     # the ask we would pay
    available: int       # contracts available at that price


@dataclass
class Opportunity:
    kind: str                    # "yes_no_spread" | "event_basket"
    event_ticker: str
    legs: List[Leg] = field(default_factory=list)
    contracts: int = 0
    cost_cents: int = 0
    fees_cents: int = 0
    payout_cents: int = 0
    detail: str = ""

    @property
    def profit_cents(self):
        return self.payout_cents - self.cost_cents - self.fees_cents

    @property
    def profit_pct(self):
        if self.cost_cents <= 0:
            return 0.0
        return 100.0 * self.profit_cents / (self.cost_cents + self.fees_cents)

    def summary(self):
        legs = ", ".join(f"{l.side.upper()} {l.ticker}@{l.price_cents}c"
                         for l in self.legs)
        return (f"[{self.kind}] {self.event_ticker} x{self.contracts} | {legs} "
                f"| cost {self.cost_cents}c + fee {self.fees_cents}c "
                f"-> payout {self.payout_cents}c "
                f"= {self.profit_cents:+d}c ({self.profit_pct:.2f}%)")


def _best_ask(orderbook, side):
    """Return (price_cents, size) of the best ask for a side, or None.

    Kalshi's orderbook returns resting BIDS for yes and no. The cheapest way to
    BUY yes is to cross the no bids: ask_yes = 100 - best_no_bid. Using the raw
    'yes' book as an ask is a classic and expensive mistake.
    """
    book = (orderbook or {}).get("orderbook") or {}
    other = "no" if side == "yes" else "yes"
    levels = book.get(other) or []
    if not levels:
        return None
    # Levels are [price, size]; the best bid on the other side is the highest.
    best = max(levels, key=lambda lv: lv[0])
    price, size = int(best[0]), int(best[1])
    return 100 - price, size


def find_yes_no_spread(market_ticker, orderbook, min_profit_pct=1.0,
                       min_liquidity=1, max_contracts=100,
                       event_ticker=""):
    """Detect ask_yes + ask_no < 100 - fees on a single market."""
    yes = _best_ask(orderbook, "yes")
    no = _best_ask(orderbook, "no")
    if not yes or not no:
        return None

    ask_yes, size_yes = yes
    ask_no, size_no = no
    if ask_yes <= 0 or ask_no <= 0:
        return None

    contracts = min(size_yes, size_no, max_contracts)
    if contracts < min_liquidity:
        return None

    cost = (ask_yes + ask_no) * contracts
    fees = (trading_fee_cents(contracts, ask_yes)
            + trading_fee_cents(contracts, ask_no))
    payout = 100 * contracts

    opp = Opportunity(
        kind="yes_no_spread",
        event_ticker=event_ticker or market_ticker,
        legs=[Leg(market_ticker, "yes", ask_yes, size_yes),
              Leg(market_ticker, "no", ask_no, size_no)],
        contracts=contracts, cost_cents=cost,
        fees_cents=fees, payout_cents=payout,
        detail=f"ask_yes {ask_yes}c + ask_no {ask_no}c = {ask_yes + ask_no}c",
    )
    if opp.profit_cents <= 0 or opp.profit_pct < min_profit_pct:
        return None
    return opp


def find_event_basket(event_ticker, market_books, min_profit_pct=1.0,
                      min_liquidity=1, max_contracts=100):
    """Detect sum(ask_yes) < 100 - fees across mutually exclusive outcomes.

    market_books: list of (ticker, orderbook). Only valid when the outcomes are
    genuinely mutually exclusive AND collectively exhaustive; caller must
    guarantee that (see BotManager, which requires an explicit allowlist).
    """
    legs, sizes = [], []
    for ticker, book in market_books:
        ask = _best_ask(book, "yes")
        if not ask:
            return None
        price, size = ask
        legs.append(Leg(ticker, "yes", price, size))
        sizes.append(size)

    if len(legs) < 2:
        return None

    contracts = min(min(sizes), max_contracts)
    if contracts < min_liquidity:
        return None

    total_ask = sum(l.price_cents for l in legs)
    cost = total_ask * contracts
    fees = sum(trading_fee_cents(contracts, l.price_cents) for l in legs)
    payout = 100 * contracts

    opp = Opportunity(
        kind="event_basket", event_ticker=event_ticker, legs=legs,
        contracts=contracts, cost_cents=cost, fees_cents=fees,
        payout_cents=payout,
        detail=f"sum(ask_yes) over {len(legs)} outcomes = {total_ask}c",
    )
    if opp.profit_cents <= 0 or opp.profit_pct < min_profit_pct:
        return None
    return opp
