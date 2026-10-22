"""Parsers for Binance read-only responses (module 2). Payload shapes follow the Binance Spot docs."""
from decimal import Decimal as Dec

import pytest

from bot.exchange.market import (
    ExchangeDataError,
    clock_skew_ms,
    parse_account,
    parse_book_ticker,
    parse_commission,
    parse_klines,
)


def kline_row(open_time=1_700_000_000_000, o="80000", h="80500", lo="79800", c="80200", minutes=60):
    close_time = open_time + minutes * 60_000 - 1
    return [open_time, o, h, lo, c, "12.5", close_time, "1002500.0", 4321, "6.1", "489000.0", "0"]


def test_book_ticker_parsed_with_spread():
    bt = parse_book_ticker({"symbol": "BTCUSDT", "bidPrice": "79990.00", "bidQty": "1.2",
                            "askPrice": "80000.00", "askQty": "0.8"}, received_ms=5)
    assert bt.bid == Dec("79990.00") and bt.ask == Dec("80000.00") and bt.received_ms == 5
    assert Dec("0.0125") < bt.spread_pct < Dec("0.0126")


@pytest.mark.parametrize("bid,ask", [("0", "80000"), ("80000", "0"), ("80001", "80000")])
def test_book_ticker_empty_or_crossed_book_is_an_error(bid, ask):
    with pytest.raises(ExchangeDataError):
        parse_book_ticker({"symbol": "BTCUSDT", "bidPrice": bid, "bidQty": "1", "askPrice": ask, "askQty": "1"}, 0)


@pytest.mark.parametrize("bad", [{}, {"symbol": "BTCUSDT"}, {"symbol": "BTCUSDT", "bidPrice": "x", "bidQty": "1",
                                                             "askPrice": "1", "askQty": "1"},
                                 {"symbol": "BTCUSDT", "bidPrice": None, "bidQty": "1", "askPrice": "1", "askQty": "1"},
                                 {"symbol": "BTCUSDT", "bidPrice": "NaN", "bidQty": "1", "askPrice": "1", "askQty": "1"}])
def test_missing_or_malformed_fields_never_become_zero(bad):
    with pytest.raises(ExchangeDataError):
        parse_book_ticker(bad, 0)


def test_klines_parsed_as_decimals():
    ks = parse_klines([kline_row(), kline_row(open_time=1_700_003_600_000)])
    assert len(ks) == 2 and ks[0].close == Dec("80200") and isinstance(ks[0].quote_volume, Dec)
    assert ks[0].trades == 4321


def test_klines_drop_unclosed_candle_no_look_ahead():
    rows = [kline_row(), kline_row(open_time=1_700_003_600_000)]
    now = 1_700_003_600_000 + 10 * 60_000          # 10 minutes into the second candle
    ks = parse_klines(rows, now_ms=now)
    assert [k.open_time for k in ks] == [1_700_000_000_000]


@pytest.mark.parametrize("row", [
    kline_row(h="79000"),                           # high below open/close
    kline_row(lo="80300"),                          # low above open
    [1, "1", "1", "1", "1"],                        # too short
    kline_row()[:6] + [0] + kline_row()[7:],        # close time before open
])
def test_inconsistent_klines_rejected(row):
    with pytest.raises(ExchangeDataError):
        parse_klines([row])


def test_klines_out_of_order_rejected():
    with pytest.raises(ExchangeDataError):
        parse_klines([kline_row(open_time=1_700_003_600_000), kline_row()])


def test_commission_adds_standard_special_and_tax_and_ignores_discount():
    c = parse_commission({
        "symbol": "BTCUSDT",
        "standardCommission": {"maker": "0.00100000", "taker": "0.00100000", "buyer": "0", "seller": "0"},
        "specialCommission": {"maker": "0.00010000", "taker": "0.00020000", "buyer": "0", "seller": "0"},
        "taxCommission": {"maker": "0", "taker": "0.00005000", "buyer": "0", "seller": "0"},
        "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.75"},
    })
    assert c.maker == Dec("0.0011") and c.taker == Dec("0.00125")


def test_commission_without_standard_block_is_an_error():
    with pytest.raises(ExchangeDataError):
        parse_commission({"symbol": "BTCUSDT"})


def test_account_balances_and_flags():
    a = parse_account({"accountType": "SPOT", "canTrade": True, "canWithdraw": False, "permissions": ["SPOT"],
                       "updateTime": 7, "balances": [{"asset": "USDT", "free": "1000.5", "locked": "0"},
                                                     {"asset": "BTC", "free": "0.01", "locked": "0.002"}]})
    assert a.account_type == "SPOT" and not a.can_withdraw
    assert a.balances[1].total == Dec("0.012")


def test_negative_balance_rejected():
    with pytest.raises(ExchangeDataError):
        parse_account({"balances": [{"asset": "BTC", "free": "-1", "locked": "0"}]})


def test_clock_skew_uses_request_midpoint():
    assert clock_skew_ms(1000, 1100, 1200) == 0
    assert clock_skew_ms(1000, 1700, 1200) == 600
    assert clock_skew_ms(1000, 500, 1000) == 500
    with pytest.raises(ExchangeDataError):
        clock_skew_ms(1000, 1000, 900)
