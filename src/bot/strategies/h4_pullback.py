"""H4: hourly pullbacks in an uptrend (docs/h4-plan.md, approved by Sultan on 24 Oct 2026 and
frozen; any change here is a new strategy with a new forward test).

On 1h candles, with daily candles built from them (finished UTC days only), for each coin:
- uptrend: the last daily close is above its 50-day average, and that average is higher
  than it was 10 days earlier;
- sharp drop: the z-score of the last 1h close against the last 24 hourly closes is below
  -`dip`, and the hour before was not (only the first hour of a drop counts);
- entry on uptrend and sharp drop; limit 0.10% above the last close (as every strategy);
- stop `stop_atr` x daily ATR(14) below the entry; take-profit 3x the stop distance; time
  stop 72 hours;
- exit at market once a day, at the first 1h close after 00:00 UTC, when the last daily
  close is below its 50-day average. No early "it bounced" exit: a market sale costs
  0.6% to 1.1% in the simulator (plan, section 2).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr, sma, zscore
from bot.analysis.resample import DAY_MS, daily
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade, entry_limit, make_intent

TREND_DAYS = 50                  # fixed: daily average for the uptrend
SLOPE_DAYS = 10                  # fixed: the average must be higher than this many days earlier
DROP_HOURS = 24                  # fixed: z-score window of hourly closes
ATR_N = 14                       # fixed (daily)
HORIZON_HOURS = 72               # fixed: time stop
HOUR_MS = 3_600_000


def new_day(now: float) -> bool:
    """True at the first 1h close after midnight UTC (a new daily candle just finished)."""
    return int(now * 1000) % DAY_MS < HOUR_MS


@dataclass(frozen=True)
class TrendPullback:
    symbols: list[str]
    dip: Decimal = Decimal("2.0")            # tunable: standard deviations below the 24h average
    stop_atr: Decimal = Decimal("1.0")       # tunable: stop distance in daily ATRs
    name: str = "H4"
    interval: str = "1h"

    def params(self) -> dict[str, object]:
        return {"dip": self.dip, "stop_atr": self.stop_atr}

    def with_params(self, **p: object) -> "TrendPullback":
        return replace(self, **p)

    def history_needed(self) -> int:
        # the 50-day average today and 10 days earlier, plus the day still running and 3 spare days for gaps
        return (TREND_DAYS + SLOPE_DAYS + 1 + 3) * 24

    def uptrend(self, days: list[Candle]) -> bool:
        if len(days) < TREND_DAYS + SLOPE_DAYS:
            return False                     # not enough history: no entry (fail closed)
        avg = sma([d.close for d in days], TREND_DAYS)
        return days[-1].close > avg[-1] and avg[-1] > avg[-1 - SLOPE_DAYS]

    def sharp_drop(self, hours: list[Candle]) -> bool:
        if len(hours) < DROP_HOURS + 1:
            return False
        z = zscore([c.close for c in hours[-(DROP_HOURS + 1):]], DROP_HOURS)
        return z[-1] < -self.dip and not z[-2] < -self.dip

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        days = daily(candles)
        a = atr(days[-ATR_N * 3:], ATR_N)[-1] if len(days) >= ATR_N else None
        return None if a is None or a <= 0 else (entry - self.stop_atr * a, HORIZON_HOURS)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            # the cheap hourly check first; the daily trend only when a drop starts (speed only; same result)
            if not self.sharp_drop(data.closed_candles(s, self.interval, limit=DROP_HOURS + 1)):
                continue
            cs = data.closed_candles(s, self.interval, limit=self.history_needed())
            if not self.uptrend(daily(cs)):
                continue
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1], "the daily close falls below its 50-day average")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        if not new_day(now):
            return []
        out: list[Action] = []
        for t in open_trades:
            days = daily(data.closed_candles(t.symbol, self.interval, limit=self.history_needed()))
            if len(days) < TREND_DAYS:
                continue
            if days[-1].close < sma([d.close for d in days], TREND_DAYS)[-1]:
                out.append(Exit(t.symbol, "the daily close fell below its 50-day average"))
        return out
