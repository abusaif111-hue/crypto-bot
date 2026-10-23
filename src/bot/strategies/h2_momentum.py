"""H2: time-series momentum on daily data (design package).

Once a day (at the first 4h close after midnight UTC), hold a coin whose return
over the last `lookback_weeks` is positive and above `threshold` x its daily
volatility x the square root of the lookback days. Exit at market when that
return turns negative. Stop `atr_mult` x daily ATR(14) below the entry; time
stop 30 days. Expected to fail in sharp reversals.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from bot.analysis.indicators import atr, realized_vol
from bot.analysis.resample import DAY_MS, daily
from bot.core.money import D
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade, entry_limit, make_intent

VOL_DAYS = 30            # fixed
ATR_N = 14               # fixed (daily)
HORIZON_HOURS = 720      # fixed: 30 days


@dataclass(frozen=True)
class TimeSeriesMomentum:
    symbols: list[str]
    lookback_weeks: int = 4                  # tunable
    threshold: Decimal = Decimal("0.5")      # tunable: in units of volatility x sqrt(days)
    atr_mult: Decimal = Decimal("3")         # tunable: stop distance in daily ATRs
    name: str = "H2"
    interval: str = "4h"
    _daily_cache: dict = field(default_factory=dict, compare=False, repr=False)

    def params(self) -> dict[str, object]:
        return {"lookback_weeks": self.lookback_weeks, "threshold": self.threshold, "atr_mult": self.atr_mult}

    def with_params(self, **p: object) -> "TimeSeriesMomentum":
        return replace(self, _daily_cache={}, **p)

    def history_needed(self) -> int:
        return (max(self.lookback_weeks * 7, VOL_DAYS) + ATR_N + 3) * 6

    def _days(self, data: CandleSource, s: str) -> list[Candle]:
        return self._daily(data.closed_candles(s, self.interval, limit=self.history_needed()))

    def _daily(self, cs: list[Candle]) -> list[Candle]:
        """Daily candles, built once per set of finished candles (speed only; same result)."""
        if not cs:
            return []
        key = (cs[0].open_time, cs[0].close, cs[-1].open_time, cs[-1].close, len(cs))
        if key not in self._daily_cache:
            if len(self._daily_cache) > 64:
                self._daily_cache.clear()
            self._daily_cache[key] = daily(cs)
        return self._daily_cache[key]

    def _momentum(self, days: list[Candle]) -> Decimal | None:
        n = self.lookback_weeks * 7
        if len(days) < max(n, VOL_DAYS) + 1:
            return None
        return days[-1].close / days[-1 - n].close - 1

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        days = self._daily(candles)
        a = atr(days[-ATR_N * 3:], ATR_N)[-1] if len(days) >= ATR_N else None
        return None if a is None or a <= 0 else (entry - self.atr_mult * a, HORIZON_HOURS)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        if not _new_day(now):
            return []
        out = []
        for s in self.symbols:
            cs = data.closed_candles(s, self.interval, limit=self.history_needed())
            days = self._daily(cs)
            mom = self._momentum(days)
            vol = realized_vol([d.close for d in days[-VOL_DAYS - 1:]], VOL_DAYS)[-1] if mom is not None else None
            if mom is None or vol is None or mom <= self.threshold * vol * D(self.lookback_weeks * 7).sqrt():
                continue
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1],
                                f"{self.lookback_weeks}-week return turns negative")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        if not _new_day(now):
            return []
        out: list[Action] = []
        for t in open_trades:
            mom = self._momentum(self._days(data, t.symbol))
            if mom is not None and mom < 0:
                out.append(Exit(t.symbol, f"{self.lookback_weeks}-week return turned negative"))
        return out


def _new_day(now: float) -> bool:
    """True at the first 4h close after midnight UTC (a new daily candle just finished)."""
    return int(now * 1000) % DAY_MS < 4 * 3_600_000
