"""Module 3: the fill model. Every assumption leans against the bot."""
from decimal import Decimal as Dec

import pytest

from bot.paper import fills


def test_taker_fee_is_exactly_the_rate_times_notional():
    assert fills.taker_fee(Dec("1000"), Dec("0.001")) == Dec("1")
    assert fills.taker_fee(Dec("0.00966") * Dec("80040"), Dec("0.001")) == Dec("0.7731864")


def test_slippage_makes_buys_dearer_and_sells_cheaper():
    assert fills.buy_fill_price(Dec("80000"), Dec("0.05")) == Dec("80040")
    assert fills.sell_fill_price(Dec("80000"), Dec("0.5")) == Dec("79600")


def test_stop_fills_from_the_stop_or_below_it_after_a_gap():
    assert fills.stop_fill_price(Dec("78000"), Dec("78000"), Dec("0.5")) == Dec("77610")
    assert fills.stop_fill_price(Dec("78000"), Dec("77000"), Dec("0.5")) == Dec("76615")
    assert fills.stop_fill_price(Dec("78000"), Dec("78500"), Dec("0.5")) == Dec("77610")   # never better than the stop


@pytest.mark.parametrize("bid, expected", [
    ("78000", (fills.STOP_LOSS, Dec("78000"))),
    ("77000", (fills.STOP_LOSS, Dec("77000"))),
    ("86000", (fills.TAKE_PROFIT, Dec("86000"))),
    ("90000", (fills.TAKE_PROFIT, Dec("86000"))),   # a limit sell fills at its price
    ("80000", None),
])
def test_book_exit(bid, expected):
    assert fills.book_exit(Dec(bid), Dec("78000"), Dec("86000")) == expected


@pytest.mark.parametrize("o, h, l, expected", [
    ("80000", "81000", "79000", None),
    ("80000", "87000", "77000", (fills.STOP_LOSS, Dec("78000"))),   # both touched: stop first
    ("77500", "87000", "77000", (fills.STOP_LOSS, Dec("77500"))),   # opened below the stop
    ("80000", "86000", "79000", None),                               # touching the target is not enough
    ("80000", "86000.01", "79000", (fills.TAKE_PROFIT, Dec("86000"))),
    ("80000", "81000", "78000", (fills.STOP_LOSS, Dec("78000"))),
])
def test_candle_exit(o, h, l, expected):
    assert fills.candle_exit(Dec(o), Dec(h), Dec(l), Dec("78000"), Dec("86000")) == expected
