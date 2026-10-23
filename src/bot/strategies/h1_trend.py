"""H1: trend-following on 4h candles with a daily filter (design package).

Buy when a 4h candle closes above the high of the previous `range_candles`
candles while the daily trend is up (last daily close above its 50-day
average). Stop `atr_mult` x ATR(14) below the entry, trailed up behind the
highest close since entry by the same distance; time stop after `horizon_hours`.
Expected to fail in choppy sideways markets.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr
from bot.analysis.resample import daily
from bot.core.money import ZERO
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, AtrExit, CandleSource, OpenTrade, RaiseStop, entry_limit, make_intent

TREND_DAYS = 50          # fixed: daily trend filter
ATR_N = 14               # fixed


@dataclass(frozen=True)
class TrendBreakout:
    symbols: list[str]
    range_candles: int = 30                  # tunable: 30 x 4h = 5 days
    atr_mult: Decimal = Decimal("2.5")       # tunable: stop and trailing distance
    horizon_hours: int = 168                 # tunable: 7 days
    name: str = "H1"
    interval: str = "4h"

    def params(self) -> dict[str, object]:
        return {"range_candles": self.range_candles, "atr_mult": self.atr_mult, "horizon_hours": self.horizon_hours}

    def with_params(self, **p: object) -> "TrendBreakout":
        return replace(self, **p)

    def history_needed(self) -> int:
        return max(self.range_candles + 1, ATR_N * 3, (TREND_DAYS + 2) * 6, self.horizon_hours // 4 + ATR_N * 3)

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        stop = AtrExit(self.atr_mult, self.horizon_hours, ATR_N).stop_for(candles, entry)
        return None if stop is None else (stop, self.horizon_hours)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            cs = data.closed_candles(s, self.interval, limit=self.history_needed())
            if len(cs) < self.history_needed():
                continue
            prior_high = max(c.high for c in cs[-self.range_candles - 1:-1])
            if cs[-1].close <= prior_high or not trend_up(cs):
                continue
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1],
                                f"4h close back below the {self.range_candles}-candle range high it broke")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        out: list[Action] = []
        for t in open_trades:
            cs = data.closed_candles(t.symbol, self.interval, limit=self.history_needed())
            since = [c for c in cs if c.open_time >= t.opened_at * 1000]
            a = atr(cs[-ATR_N * 3:], ATR_N)[-1] if len(cs) >= ATR_N else None
            if not since or a is None:
                continue
            trail = max(c.close for c in since) - self.atr_mult * a
            if trail > t.stop_price:
                out.append(RaiseStop(t.symbol, trail))
        return out


def trend_up(candles: list[Candle]) -> bool:
    days = daily(candles)
    if len(days) < TREND_DAYS:
        return False
    avg = sum((d.close for d in days[-TREND_DAYS:]), ZERO) / TREND_DAYS
    return days[-1].close > avg
