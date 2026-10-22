"""Market data service (module 3).

Builds the `MarketSnapshot` the risk engine needs from any `SpotReadVenue`:
the read-only Binance adapter for paper trading on live prices, or a fake in
tests. It never places orders; it only calls the venue's read methods.

Fail closed: a failed request, a missing symbol or a broken book raises
`MarketDataUnavailable`, and callers must treat that as "no new entries".
Slow-moving inputs (exchange rules, 24h volume, clock skew, average price) are
cached; the book and depth are fetched fresh for every snapshot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from bot.core.config import AppConfig
from bot.core.money import D
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Book, Candle, SpotReadVenue
from bot.risk.models import MarketSnapshot


class MarketDataUnavailable(RuntimeError):
    pass


@dataclass
class MarketDataService:
    venue: SpotReadVenue
    cfg: AppConfig
    clock: Callable[[], float] = time.time
    rules_ttl_s: float = 3600        # exchangeInfo rarely changes; refresh hourly
    slow_ttl_s: float = 300          # 24h volume and clock skew
    reference_ttl_s: float = 60      # Binance's 5-minute average price
    depth_within_pct: Decimal = Decimal("0.5")
    _cache: dict[tuple[str, ...], tuple[float, Any]] = field(default_factory=dict, repr=False)

    def _cached(self, key: tuple[str, ...], ttl: float, fetch: Callable[[], Any]) -> Any:
        now = self.clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        value = _fetch(key[0], fetch)
        self._cache[key] = (now, value)
        return value

    def rules(self, symbol: str) -> SymbolRules:
        return self._cached(("exchange rules", symbol), self.rules_ttl_s,
                            lambda: self.venue.symbol_rules([symbol])[symbol])

    def books(self, symbols: list[str]) -> dict[str, Book]:
        books = _fetch("book", lambda: self.venue.books(list(symbols)))
        for s in symbols:
            b = books.get(s)
            if b is None:
                raise MarketDataUnavailable(f"book: no prices for {s}")
            if b.bid <= 0 or b.ask <= 0 or b.ask < b.bid:
                raise MarketDataUnavailable(f"book: {s} has an unusable book (bid {b.bid}, ask {b.ask})")
        return {s: books[s] for s in symbols}

    def snapshot(self, symbol: str) -> MarketSnapshot:
        t0 = self.clock()
        book = self.books([symbol])[symbol]
        depth = _fetch("depth", lambda: self.venue.ask_depth_quote(symbol, self.depth_within_pct))
        volume = self._cached(("24h volume", symbol), self.slow_ttl_s,
                              lambda: self.venue.quote_volume_24h([symbol])[symbol])
        reference = self._cached(("average price", symbol), self.reference_ttl_s,
                                 lambda: self.venue.avg_price(symbol))
        skew = self._cached(("clock skew",), self.slow_ttl_s, lambda: self.venue.clock_skew_ms())
        # Age counts from when the book request was sent, so slow follow-up calls make data older, not newer.
        age = D(max(0.0, self.clock() - t0)).quantize(Decimal("0.001"))
        return MarketSnapshot(best_bid=book.bid, best_ask=book.ask, quote_volume_24h=D(volume),
                              ask_depth_quote=D(depth), data_age_s=age, clock_skew_ms=int(skew),
                              reference_price=D(reference))

    def closed_candles(self, symbol: str, interval: str, start_ms: int | None = None,
                       end_ms: int | None = None, limit: int = 500) -> list[Candle]:
        """Only finished candles: a candle still forming must never feed a decision (no look-ahead)."""
        candles = _fetch("candles", lambda: self.venue.klines(symbol, interval, start_ms, end_ms, limit))
        return [c for c in candles if c.closed]


def _fetch(what: str, fetch: Callable[[], Any]) -> Any:
    try:
        return fetch()
    except MarketDataUnavailable:
        raise
    except Exception as e:  # any venue error blocks entries; the message is kept, nothing else
        raise MarketDataUnavailable(f"{what}: {type(e).__name__}: {e}") from None
