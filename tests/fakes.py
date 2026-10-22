"""An in-memory market venue for module 3 tests. No network."""
from __future__ import annotations

from decimal import Decimal as Dec

from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Book
from tests.conftest import exchange_info_symbol


class Clock:
    def __init__(self, t: float = 1_792_627_200.0):   # 2026-10-22 00:00 UTC, a Thursday
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeMarket:
    """Implements SpotReadVenue's market-data methods with settable values."""

    def __init__(self, symbols=("BTCUSDT", "ETHUSDT")):
        self.rules = {s: parse_symbol(exchange_info_symbol(s)) for s in symbols}
        self.book = {"BTCUSDT": Book(Dec("79990"), Dec("80000")), "ETHUSDT": Book(Dec("2499.9"), Dec("2500"))}
        self.volume = {s: Dec("1500000000") for s in symbols}
        self.depth = {s: Dec("2000000") for s in symbols}
        self.avg = {"BTCUSDT": Dec("80000"), "ETHUSDT": Dec("2500")}
        self.skew = 20
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def set_bid(self, symbol: str, bid, spread="10") -> None:
        self.book[symbol] = Book(Dec(str(bid)), Dec(str(bid)) + Dec(spread))

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    def server_time_ms(self):
        self._call("time")
        return 1_792_627_200_000

    def clock_skew_ms(self, samples=5):
        self._call("skew")
        return self.skew

    def symbol_rules(self, symbols):
        self._call("rules")
        return {s: self.rules[s] for s in symbols}

    def books(self, symbols):
        self._call("books")
        return {s: self.book[s] for s in symbols if s in self.book}

    def quote_volume_24h(self, symbols):
        self._call("volume")
        return {s: self.volume[s] for s in symbols}

    def ask_depth_quote(self, symbol, within_pct=Dec("0.5"), limit=100):
        self._call("depth")
        return self.depth[symbol]

    def avg_price(self, symbol):
        self._call("avg")
        return self.avg[symbol]

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self._call("klines")
        return []

    def balances(self):
        raise AssertionError("the simulator must never read real balances")

    def taker_fee(self, symbol):
        raise AssertionError("the simulator must never read the real fee")
