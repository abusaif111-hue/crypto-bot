"""Module 3: market data service. Fails closed; caches slow inputs; works on the real adapter."""
from decimal import Decimal as Dec

import pytest

from bot.core.config import AppConfig
from bot.exchange.binance import BinanceReadOnly
from bot.exchange.interface import Book, Candle, SpotReadVenue
from bot.market.data import MarketDataService, MarketDataUnavailable
from tests.fakes import Clock, FakeMarket
from tests.unit.test_binance_readonly import PUBLIC, FakeClient

AVG_PRICE = {"mins": 5, "price": "82010.11000000", "closeTime": 1792667463000}   # documented shape


def service(market=None, clock=None):
    return MarketDataService(market or FakeMarket(), AppConfig(), clock or Clock())


def test_snapshot_has_everything_the_risk_engine_needs():
    snap = service().snapshot("BTCUSDT")
    assert (snap.best_bid, snap.best_ask) == (Dec("79990"), Dec("80000"))
    assert snap.quote_volume_24h == Dec("1500000000") and snap.ask_depth_quote == Dec("2000000")
    assert snap.reference_price == Dec("80000") and snap.clock_skew_ms == 20
    assert snap.data_age_s == 0


def test_data_age_counts_from_the_book_request():
    clock = Clock()
    m = FakeMarket()
    real_depth = m.ask_depth_quote

    def slow_depth(*a, **k):
        clock.advance(2.5)
        return real_depth(*a, **k)

    m.ask_depth_quote = slow_depth
    assert service(m, clock).snapshot("BTCUSDT").data_age_s == Dec("2.500")


def test_slow_inputs_are_cached_book_and_depth_are_not():
    clock, m = Clock(), FakeMarket()
    svc = service(m, clock)
    svc.snapshot("BTCUSDT")
    svc.rules("BTCUSDT")
    svc.snapshot("BTCUSDT")
    svc.rules("BTCUSDT")
    assert m.calls.count("books") == 2 and m.calls.count("depth") == 2
    assert m.calls.count("volume") == 1 and m.calls.count("skew") == 1
    assert m.calls.count("avg") == 1 and m.calls.count("rules") == 1
    clock.advance(301)
    svc.snapshot("BTCUSDT")
    assert m.calls.count("volume") == 2 and m.calls.count("skew") == 2 and m.calls.count("rules") == 1
    clock.advance(3600)
    svc.rules("BTCUSDT")
    assert m.calls.count("rules") == 2


@pytest.mark.parametrize("failing", ["books", "depth", "volume", "avg", "skew"])
def test_any_failure_fails_closed(failing):
    m = FakeMarket()
    m.fail.add(failing)
    with pytest.raises(MarketDataUnavailable):
        service(m).snapshot("BTCUSDT")


@pytest.mark.parametrize("book", [Book(Dec("0"), Dec("1")), Book(Dec("100"), Dec("99")), None])
def test_broken_or_missing_book_fails_closed(book):
    m = FakeMarket()
    if book is None:
        del m.book["BTCUSDT"]
    else:
        m.book["BTCUSDT"] = book
    with pytest.raises(MarketDataUnavailable):
        service(m).snapshot("BTCUSDT")


def test_only_closed_candles_are_returned():
    m = FakeMarket()
    c = dict(open_time=0, open=Dec(1), high=Dec(1), low=Dec(1), close=Dec(1), volume=Dec(1), close_time=1,
             quote_volume=Dec(1))
    m.klines = lambda *a, **k: [Candle(**c, closed=True), Candle(**c, closed=False)]
    assert [x.closed for x in service(m).closed_candles("BTCUSDT", "1h")] == [True]


def test_works_on_the_real_read_only_adapter():
    routes = {**PUBLIC, "/api/v3/avgPrice": AVG_PRICE}
    client = FakeClient(routes)
    ex = BinanceReadOnly(public_client=client, clock=lambda: 1792667463.9, _sleep=lambda s: None)
    assert isinstance(ex, SpotReadVenue)
    snap = MarketDataService(ex, AppConfig(), clock=lambda: 1792667463.9).snapshot("BTCUSDT")
    assert snap.best_bid == Dec("82003.39") and snap.best_ask == Dec("82003.40")
    assert snap.reference_price == Dec("82010.11") and snap.quote_volume_24h > 0 and snap.ask_depth_quote > 0
    assert ("/api/v3/avgPrice", False, {"symbol": "BTCUSDT"}) in client.calls
    assert all(signed is False for _, signed, _ in client.calls)   # market data never needs the key
