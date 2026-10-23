"""H2b: blended time-series momentum with a volatility pause (docs/h2b-plan.md, approved
by Sultan and frozen; any change here is a new strategy with a new forward test).

Once a day (at the first 4h close after midnight UTC), for each coin:
- `vol` is the 30-day volatility of daily closes; for each lookback L of 14, 28 and 56
  days, the return over L days is "strong" when above `threshold` x vol x sqrt(L);
- volatility pause: today's vol must be below the 80th percentile of the coin's own
  30-day vol on the 365 daily candles before today (fewer than 365: no entry);
- entry when at least 2 of the 3 lookbacks are strong and there is no pause;
- exit at market when at least 2 of the 3 returns are below zero. The pause never
  forces an exit.
Stop `atr_mult` x daily ATR(14) below the entry; take-profit 3x the stop distance;
time stop 30 days. Built after H2 failed the holdout, so its real test is forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from bot.analysis.indicators import atr, percentile, realized_vol
from bot.analysis.resample import daily
from bot.core.money import D
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade, entry_limit, make_intent
from bot.strategies.h2_momentum import _new_day

LOOKBACK_DAYS = (14, 28, 56)     # fixed
AGREE = 2                        # fixed: lookbacks that must agree
VOL_DAYS = 30                    # fixed
PAUSE_DAYS = 365                 # fixed: history for the volatility pause
PAUSE_PERCENTILE = Decimal("0.8")  # fixed
ATR_N = 14                       # fixed (daily)
HORIZON_HOURS = 720              # fixed: 30 days
_SQRT = {n: D(n).sqrt() for n in LOOKBACK_DAYS}


@dataclass(frozen=True)
class BlendedMomentum:
    symbols: list[str]
    threshold: Decimal = Decimal("0.5")      # tunable: in units of volatility x sqrt(days)
    atr_mult: Decimal = Decimal("3")         # tunable: stop distance in daily ATRs
    name: str = "H2b"
    interval: str = "4h"
    _daily_cache: dict = field(default_factory=dict, compare=False, repr=False)
    _vol_cache: dict = field(default_factory=dict, compare=False, repr=False)

    def params(self) -> dict[str, object]:
        return {"threshold": self.threshold, "atr_mult": self.atr_mult}

    def with_params(self, **p: object) -> "BlendedMomentum":
        return replace(self, _daily_cache={}, _vol_cache={}, **p)

    def history_needed(self) -> int:
        # today plus 365 earlier days with a 30-day vol each, plus 3 spare days for maintenance gaps
        return (VOL_DAYS + PAUSE_DAYS + 1 + 3) * 6

    def _daily(self, cs: list[Candle]) -> list[Candle]:
        """Daily candles, built once per set of finished candles (speed only; same result)."""
        if not cs:
            return []
        key = (cs[0].open_time, cs[0].close, cs[-1].open_time, cs[-1].close, len(cs))
        if key not in self._daily_cache:
            if len(self._daily_cache) > 16:          # a year of daily candles each: keep few (512 MB server)
                self._daily_cache.clear()
            self._daily_cache[key] = daily(cs)
        return self._daily_cache[key]

    def _days(self, data: CandleSource, s: str) -> list[Candle]:
        return self._daily(data.closed_candles(s, self.interval, limit=self.history_needed()))

    def _vol(self, days: list[Candle], i: int) -> Decimal | None:
        """30-day vol on day i, from days i-30..i only. Cached by the window's first and last day
        (speed only; one strategy copy sees one price history)."""
        if i < VOL_DAYS:
            return None
        a, b = days[i - VOL_DAYS], days[i]
        key = (a.open_time, a.close, b.open_time, b.close)
        if key not in self._vol_cache:
            if len(self._vol_cache) > 4_000:
                self._vol_cache.clear()
            self._vol_cache[key] = realized_vol([d.close for d in days[i - VOL_DAYS:i + 1]], VOL_DAYS)[-1]
        return self._vol_cache[key]

    @staticmethod
    def _returns(days: list[Candle]) -> list[Decimal] | None:
        if len(days) < max(LOOKBACK_DAYS) + 1:
            return None
        return [days[-1].close / days[-1 - n].close - 1 for n in LOOKBACK_DAYS]

    def signal(self, days: list[Candle]) -> tuple[bool, str]:
        """(enter, why not). Uses only the finished daily candles given."""
        rets = self._returns(days)
        if rets is None or len(days) < VOL_DAYS + PAUSE_DAYS + 1:
            return False, "not enough history"
        today = len(days) - 1
        vol = self._vol(days, today)
        if vol is None or vol <= 0:
            return False, "volatility unknown"
        strong = sum(1 for r, n in zip(rets, LOOKBACK_DAYS) if r > self.threshold * vol * _SQRT[n])
        if strong < AGREE:
            return False, f"{strong} of {len(LOOKBACK_DAYS)} lookbacks strong"
        earlier = [self._vol(days, i) for i in range(today - PAUSE_DAYS, today)]
        if any(v is None for v in earlier):
            return False, "volatility unknown"
        if vol >= percentile(earlier, PAUSE_PERCENTILE):
            return False, "volatility pause"
        return True, ""

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
            enter, _ = self.signal(self._daily(cs))
            if not enter:
                continue
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1],
                                "2 of the 14/28/56-day returns turn negative")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        if not _new_day(now):
            return []
        out: list[Action] = []
        for t in open_trades:
            rets = self._returns(self._days(data, t.symbol))
            if rets is not None and sum(1 for r in rets if r < 0) >= AGREE:
                out.append(Exit(t.symbol, "2 of the 14/28/56-day returns turned negative"))
        return out
