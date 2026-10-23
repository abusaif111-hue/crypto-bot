"""Historical market for backtests (module 4): a `SpotReadVenue` over stored candles.

The backtester moves the replay clock forward one candle at a time. At any
moment the venue shows only what was known then:
- `klines` returns finished candles only (close time before now);
- the book is the price at "now" (the open of the candle starting now) plus or
  minus half the spread.

Assumptions the history cannot answer, stated in every report:
- Half-spread: fixed (`CostModel.half_spread_pct`).
- Order-book depth near the touch: a fixed share of 24h volume; Binance does not
  publish historical depth. The bot's orders (a few thousand USDT) are far below
  BTC/ETH depth, so this rarely binds.
- The average price used for the PERCENT_PRICE_BY_SIDE band is the current price.
- Exchange rules (tick, step, minimum order) are today's, saved at download.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from decimal import Decimal

from bot.core.money import ZERO, D, pct
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Book, Candle
from bot.portfolio.accounting import Balance


@dataclass(frozen=True)
class CostModel:
    taker_fee: Decimal = Decimal("0.001")          # 0.1% per fill
    half_spread_pct: Decimal = Decimal("0.01")     # 0.01% each side of the price
    depth_share_of_24h_volume: Decimal = Decimal("0.0005")
    entry_slippage_pct: Decimal | None = None      # None: the risk config's allowance (0.05%)
    exit_slippage_pct: Decimal | None = None       # None: the risk config's stop allowance per coin
    slippage_multiplier: Decimal = Decimal("1")

    def doubled(self) -> "CostModel":
        """Every cost twice as large: fee, spread and slippage (robustness check)."""
        return replace(self, taker_fee=self.taker_fee * 2, half_spread_pct=self.half_spread_pct * 2,
                       slippage_multiplier=self.slippage_multiplier * 2)


class ReplayMarket:
    def __init__(self, candles: dict[str, list[Candle]], rules: dict[str, SymbolRules], cost: CostModel,
                 interval: str):
        from bot.backtest.history import INTERVAL_MS

        self.candles = candles
        self.rules = rules
        self.cost = cost
        self.interval = interval
        self.interval_ms = INTERVAL_MS[interval]
        self.now_ms = 0
        self._close_times = {s: [c.close_time for c in cs] for s, cs in candles.items()}
        self._open_times = {s: [c.open_time for c in cs] for s, cs in candles.items()}

    # ------------------------------------------------------------ clock
    def clock(self) -> float:
        return self.now_ms / 1000

    def set_time(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def _finished(self, symbol: str) -> int:
        """Number of candles finished before now."""
        return bisect_left(self._close_times[symbol], self.now_ms)

    def price(self, symbol: str) -> Decimal:
        """Price at now: the open of the candle starting now, else the last finished close."""
        opens = self._open_times[symbol]
        i = bisect_left(opens, self.now_ms)
        if i < len(opens) and opens[i] == self.now_ms:
            return self.candles[symbol][i].open
        k = self._finished(symbol)
        if k == 0:
            raise LookupError(f"no {symbol} price at {self.now_ms}")
        return self.candles[symbol][k - 1].close

    # ------------------------------------------------------------ SpotReadVenue
    def server_time_ms(self) -> int:
        return self.now_ms

    def clock_skew_ms(self, samples: int = 5) -> int:
        return 0

    def symbol_rules(self, symbols: list[str]) -> dict[str, SymbolRules]:
        return {s: self.rules[s] for s in symbols}

    def books(self, symbols: list[str]) -> dict[str, Book]:
        out = {}
        for s in symbols:
            p = self.price(s)
            h = pct(self.cost.half_spread_pct)
            out[s] = Book(bid=p * (1 - h), ask=p * (1 + h))
        return out

    def quote_volume_24h(self, symbols: list[str]) -> dict[str, Decimal]:
        per_day = max(1, 86_400_000 // self.interval_ms)
        out = {}
        for s in symbols:
            k = self._finished(s)
            out[s] = sum((c.quote_volume for c in self.candles[s][max(0, k - per_day):k]), ZERO)
        return out

    def ask_depth_quote(self, symbol: str, within_pct: Decimal = Decimal("0.5"), limit: int = 100) -> Decimal:
        return self.quote_volume_24h([symbol])[symbol] * self.cost.depth_share_of_24h_volume

    def avg_price(self, symbol: str) -> Decimal:
        return self.price(symbol)

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[Candle]:
        """Finished candles only, newest last. As on Binance, `limit` counts forward from `start_ms`
        when one is given, else back from now."""
        if interval != self.interval:
            raise ValueError(f"this replay holds {self.interval} candles, not {interval}")
        k = self._finished(symbol)
        cs = self.candles[symbol]
        lo = 0 if start_ms is None else bisect_left(self._open_times[symbol], start_ms)
        hi = k if end_ms is None else min(k, bisect_right(self._open_times[symbol], end_ms))
        if start_ms is None:
            lo = max(lo, hi - limit)
        else:
            hi = min(hi, lo + limit)
        return cs[lo:hi]

    def balances(self) -> list[Balance]:
        raise RuntimeError("the replay market holds no account; balances come from the simulator")

    def taker_fee(self, symbol: str) -> Decimal:
        return D(self.cost.taker_fee)
