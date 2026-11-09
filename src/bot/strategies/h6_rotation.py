"""H6: weekly rotation into the strongest coins (docs/h6-plan.md, approved by Sultan on 9 Nov 2026 and
frozen; any change here is a new strategy).

On 4h candles, with daily candles built from them (finished UTC days only), once a week at the first 4h
close after Monday 00:00 UTC:
- uptrend filter: a coin can be held only if its daily close is above its `trend_days`-day average close;
- ranking: among the coins that pass, by return over the last `lookback_days` days; the top 2 are held;
- a held coin outside the top 2 (or below its filter) is sold at market; a top-2 coin not held is bought
  (limit at the last close + 0.10%, through the risk engine, which refuses a coin already held);
- stop `stop_atr` x daily ATR(20) below the entry; take-profit 3x the stop distance; time stop 35 days.
With no coin in an uptrend, nothing is held.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.core.money import ZERO
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade, entry_limit, make_intent

TOP_N = 2                       # fixed: coins held
ATR_DAYS = 20                   # fixed: daily ATR period
HORIZON_DAYS = 35               # fixed: time stop (five weekly rankings)
WEEK_MS = 7 * DAY_MS
MONDAY_MS = 4 * DAY_MS          # 1970-01-01 was a Thursday; the first Monday 00:00 UTC is 4 days later


@dataclass(frozen=True)
class WeeklyRotation:
    symbols: list[str]
    lookback_days: int = 28                 # tunable: the ranking's return period (days)
    trend_days: int = 100                   # tunable: the uptrend filter's average (days)
    stop_atr: Decimal = Decimal("2.5")      # tunable: stop distance in daily ATR(20)s
    name: str = "H6"
    interval: str = "4h"

    def params(self) -> dict[str, object]:
        return {"lookback_days": self.lookback_days, "trend_days": self.trend_days, "stop_atr": self.stop_atr}

    def with_params(self, **p: object) -> "WeeklyRotation":
        return replace(self, **p)

    def history_needed(self) -> int:
        days = max(self.trend_days, self.lookback_days + 1, ATR_DAYS * 3) + 3   # + the running day and 2 spare
        return days * 6

    @staticmethod
    def new_week(now: float) -> bool:
        """True at the first 4h close after Monday 00:00 UTC."""
        return (int(now * 1000) - MONDAY_MS) % WEEK_MS < INTERVAL_MS["4h"]

    def score(self, days: list[Candle]) -> Decimal | None:
        """The coin's `lookback_days` return if it is in an uptrend; None otherwise or without enough days."""
        if len(days) < max(self.trend_days, self.lookback_days + 1):
            return None
        closes = [d.close for d in days[-self.trend_days:]]
        average = sum(closes, ZERO) / len(closes)
        last = days[-1].close
        if last <= average:
            return None
        return last / days[-1 - self.lookback_days].close - 1

    def leaders(self, data: CandleSource) -> list[str]:
        """The top coins this week, strongest first (ties: the coin listed first)."""
        scored = []
        for i, s in enumerate(self.symbols):
            sc = self.score(daily(data.closed_candles(s, self.interval, limit=self.history_needed())))
            if sc is not None:
                scored.append((-sc, i, s))
        return [s for _, _, s in sorted(scored)[:TOP_N]]

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        days = daily(candles)
        a = atr(days[-ATR_DAYS * 3:], ATR_DAYS)[-1] if len(days) > ATR_DAYS else None
        return None if a is None or a <= 0 else (entry - self.stop_atr * a, HORIZON_DAYS * 24)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        if not self.new_week(now):
            return []
        out = []
        for s in self.leaders(data):
            cs = data.closed_candles(s, self.interval, limit=self.history_needed())
            entry = entry_limit(cs[-1].close)
            plan = self.exit_plan(cs, entry)
            if plan:
                i = make_intent(s, entry, plan[0], self.name, plan[1],
                                f"out of the top {TOP_N} or below its {self.trend_days}-day average at a weekly ranking")
                if i:
                    out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        """At each weekly ranking: sell every held coin that is no longer among the leaders."""
        if not self.new_week(now) or not open_trades:
            return []
        keep = set(self.leaders(data))
        return [Exit(t.symbol, f"no longer in the top {TOP_N} coins in an uptrend at the weekly ranking")
                for t in open_trades if t.symbol not in keep]
