"""Fill model for simulated orders. Pure functions, no I/O.

Every assumption leans against the bot: buys fill above the ask, stops fill
below the stop, and when one candle touches both exits the stop is taken first.
Slipped prices are average fill prices across several book levels, so they are
not rounded to the tick size.
"""
from __future__ import annotations

from decimal import Decimal

from bot.core.money import D, pct

STOP_LOSS = "STOP_LOSS"
TAKE_PROFIT = "TAKE_PROFIT"
TIME_STOP = "TIME_STOP"
STRATEGY_EXIT = "STRATEGY_EXIT"     # the strategy's own exit signal: sell at market


def taker_fee(notional: Decimal, rate: Decimal) -> Decimal:
    """Fee in USDT. Binance Spot's standard taker rate is 0.1% (rate 0.001)."""
    return D(notional) * D(rate)


def buy_fill_price(ask: Decimal, slippage_pct: Decimal) -> Decimal:
    return D(ask) * (1 + pct(slippage_pct))


def sell_fill_price(price: Decimal, slippage_pct: Decimal) -> Decimal:
    return D(price) * (1 - pct(slippage_pct))


def stop_fill_price(stop: Decimal, observed: Decimal, slippage_pct: Decimal) -> Decimal:
    """A triggered stop sells at market: from the stop, or from below it if price gapped through."""
    return sell_fill_price(min(D(stop), D(observed)), slippage_pct)


def book_exit(bid: Decimal, stop: Decimal, take_profit: Decimal) -> tuple[str, Decimal] | None:
    """Which exit leg a new best bid triggers, and the price it was observed at."""
    if bid <= stop:
        return STOP_LOSS, D(bid)
    if bid >= take_profit:
        return TAKE_PROFIT, D(take_profit)
    return None


def candle_exit(open_: Decimal, high: Decimal, low: Decimal, stop: Decimal,
                take_profit: Decimal) -> tuple[str, Decimal] | None:
    """Exit leg hit inside one finished candle. The order of prices inside a candle is
    unknown, so a candle that touches both legs counts as a stop."""
    if open_ <= stop:
        return STOP_LOSS, D(open_)          # opened below the stop: gapped through it
    if low <= stop:
        return STOP_LOSS, D(stop)
    if high > take_profit:                  # must trade through the limit to be sure it filled
        return TAKE_PROFIT, D(take_profit)
    return None
