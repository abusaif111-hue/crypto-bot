"""H3: mean reversion in range regimes (design package).

Buy an oversold 4h close (RSI(14) below `oversold`) only when the regime filter
says "range": the daily `regime_days`-day average moved less than 3% over the
last 10 days. Stop 2 x ATR(14) below the entry; exit at market once RSI is back
above 50; tight time stop after `time_stop_hours`. Expected to fail in strong
trends and crashes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import rsi
from bot.analysis.resample import daily
from bot.core.money import ZERO
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, AtrExit, CandleSource, Exit, OpenTrade, entry_limit, make_intent

RSI_N = 14                       # fixed
EXIT_RSI = Decimal("50")         # fixed: reverted to the mean
STOP_ATR = Decimal("2")          # fixed
SLOPE_DAYS = 10                  # fixed
RANGE_SLOPE = Decimal("0.03")    # fixed: average moved less than 3% over SLOPE_DAYS


@dataclass(frozen=True)
class RangeMeanReversion:
    symbols: list[str]
    oversold: Decimal = Decimal("30")        # tunable
    regime_days: int = 20                    # tunable
    time_stop_hours: int = 24                # tunable
    name: str = "H3"
    interval: str = "4h"

    def params(self) -> dict[str, object]:
        return {"oversold": self.oversold, "regime_days": self.regime_days, "time_stop_hours": self.time_stop_hours}

    def with_params(self, **p: object) -> "RangeMeanReversion":
        return replace(self, **p)

    def history_needed(self) -> int:
        return max(RSI_N * 4, (self.regime_days + SLOPE_DAYS + 2) * 6)

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        stop = AtrExit(STOP_ATR, self.time_stop_hours).stop_for(candles, entry)
        return None if stop is None else (stop, self.time_stop_hours)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            cs = data.closed_candles(s, self.interval, limit=self.history_needed())
            if len(cs) < self.history_needed():
                continue
            r = rsi([c.close for c in cs[-RSI_N * 4:]], RSI_N)[-1]
            if r is None or r >= self.oversold or not in_range(daily(cs), self.regime_days):
                continue
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1], "regime filter no longer says range")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        out: list[Action] = []
        for t in open_trades:
            cs = data.closed_candles(t.symbol, self.interval, limit=RSI_N * 4)
            r = rsi([c.close for c in cs], RSI_N)[-1] if len(cs) > RSI_N else None
            if r is not None and r > EXIT_RSI:
                out.append(Exit(t.symbol, "RSI back above 50"))
        return out


def in_range(days: list[Candle], n: int) -> bool:
    if len(days) < n + SLOPE_DAYS:
        return False
    now_avg = sum((d.close for d in days[-n:]), ZERO) / n
    then_avg = sum((d.close for d in days[-n - SLOPE_DAYS:-SLOPE_DAYS]), ZERO) / n
    return abs(now_avg / then_avg - 1) < RANGE_SLOPE
