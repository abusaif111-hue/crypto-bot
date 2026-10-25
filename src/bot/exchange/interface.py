"""The read interface every Spot venue offers (module 3).

`BinanceReadOnly` (the real exchange, read-only) and `SimExchange` (paper
trading) both implement `SpotReadVenue`, so the market data service and later
the backtester and execution code work the same on either.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from bot.exchange.filters import SymbolRules
from bot.portfolio.accounting import Balance

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
KLINES_PAGE = 1000                # Binance's maximum klines per request


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal
    closed: bool


@dataclass(frozen=True)
class Book:
    bid: Decimal
    ask: Decimal

    @property
    def spread_pct(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else Decimal("Infinity")


@runtime_checkable
class SpotReadVenue(Protocol):
    def server_time_ms(self) -> int: ...

    def clock_skew_ms(self, samples: int = 5) -> int: ...

    def symbol_rules(self, symbols: list[str]) -> dict[str, SymbolRules]: ...

    def books(self, symbols: list[str]) -> dict[str, Book]: ...

    def quote_volume_24h(self, symbols: list[str]) -> dict[str, Decimal]: ...

    def ask_depth_quote(self, symbol: str, within_pct: Decimal = Decimal("0.5"), limit: int = 100) -> Decimal: ...

    def avg_price(self, symbol: str) -> Decimal: ...

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[Candle]: ...

    def balances(self) -> list[Balance]: ...

    def taker_fee(self, symbol: str) -> Decimal: ...
