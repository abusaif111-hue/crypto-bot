"""The strategy contract (module 4). Pure: no I/O, no orders.

A strategy only proposes. It sees finished candles through the market data
service (`closed_candles`), the same call in backtests (replay venue) and paper
trading (live venue), and returns:
- `propose`: new `TradeIntent`s, which then go through `SimExchange.submit()`
  and so through the risk engine;
- `manage`: for positions already open, a tighter stop (`RaiseStop`; the
  simulator refuses to lower one) or an exit at market (`Exit`). Exits only
  reduce risk.
Each strategy has at most 3 tunable settings (design package, to limit curve fitting).

Fixed, not tuned (approved by Sultan):
- the entry limit is the last close + 0.10%, so a small move up before the next
  candle opens does not cancel the order; the risk engine sizes on that worse price;
- the take-profit is 3x the stop distance; the strategy's own exits do most of the work.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bot.analysis.indicators import atr
from bot.core.money import pct
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent

ENTRY_LIMIT_BUFFER_PCT = Decimal("0.10")
TAKE_PROFIT_R = Decimal("3")
MAX_TUNABLE = 3


class CandleSource(Protocol):
    def closed_candles(self, symbol: str, interval: str, start_ms: int | None = None,
                       end_ms: int | None = None, limit: int = 500) -> list[Candle]: ...


@dataclass(frozen=True)
class OpenTrade:
    """What a strategy may know about one of its open positions."""
    symbol: str
    entry_price: Decimal
    stop_price: Decimal
    opened_at: float


@dataclass(frozen=True)
class RaiseStop:
    symbol: str
    stop: Decimal


@dataclass(frozen=True)
class Exit:
    symbol: str
    reason: str


Action = RaiseStop | Exit


class Strategy(Protocol):
    name: str
    interval: str
    symbols: list[str]

    def params(self) -> dict[str, object]:
        """The tunable settings, at most three."""
        ...

    def with_params(self, **params: object) -> "Strategy":
        """A copy with different tunable settings (walk-forward and robustness checks)."""
        ...

    def history_needed(self) -> int:
        """How many finished candles one decision needs."""
        ...

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        """(stop, holding horizon in hours) for an entry at `entry`; random entries reuse it."""
        ...

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]: ...

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]: ...


@dataclass(frozen=True)
class AtrExit:
    """Stop a multiple of the average true range below the entry; take-profit 3x that distance; time stop."""
    atr_mult: Decimal = Decimal("2")
    horizon_hours: int = 96
    atr_n: int = 14

    def stop_for(self, candles: list[Candle], entry: Decimal) -> Decimal | None:
        if len(candles) < self.atr_n:
            return None
        a = atr(candles[-(self.atr_n * 3):], self.atr_n)[-1]
        if a is None or a <= 0:
            return None
        return entry - self.atr_mult * a


def entry_limit(last_close: Decimal) -> Decimal:
    return last_close * (1 + pct(ENTRY_LIMIT_BUFFER_PCT))


def make_intent(symbol: str, entry: Decimal, stop: Decimal | None, strategy: str, horizon_hours: int,
                invalidation: str) -> TradeIntent | None:
    if stop is None or not (0 < stop < entry):
        return None
    return TradeIntent(symbol=symbol, side="BUY", entry_price=entry, stop_price=stop,
                       take_profit_price=entry + TAKE_PROFIT_R * (entry - stop), strategy=strategy,
                       horizon_hours=horizon_hours, invalidation=invalidation)


def scaled(value, factor: Decimal):
    """A tunable setting moved by `factor` (0.8 / 1.2), keeping its type; whole numbers stay whole."""
    if isinstance(value, int):
        return max(1, int((Decimal(value) * factor).to_integral_value()))
    return Decimal(value) * factor
