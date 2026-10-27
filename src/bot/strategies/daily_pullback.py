"""D1: daily trend pullback, Spot long-only (Sultan's request, 27 Oct 2026; signals only, not wired into any runner).

Rules, on finished 4h candles, with daily candles built from them (finished UTC days only):
- daily uptrend: the last daily close is above its `trend_days`-day EMA, and that EMA is higher than it was
  5 days earlier. Anything else: NO TRADE (there are no short signals: Spot, long-only);
- pullback: one of the 6 finished 4h candles before the signal candle had its low at or below the 4h
  `pullback_ema`-EMA;
- signal: the last finished 4h candle closed above the previous 4h candle's high, and no earlier 4h candle of
  the same UTC day showed this pullback-and-resume pattern (only the day's first setup counts, even if it was
  not tradeable; at most one signal per coin per day);
- entry: limit at the last close + 0.10% (as every strategy); stop `stop_atr` x 4h ATR(14) below the entry;
  take-profit 2x the stop distance; time stop 72 hours; sold at market when the daily close falls below the EMA;
- no trade when the signal candle's range is more than 2 x ATR (a spike), the stop is more than 3% below the
  entry, or there is not enough or only stale data.

The risk engine still decides every trade: size (0.25% of equity at risk), reward-to-risk after costs (at
least 1.5), loss limits, positions and correlation groups, all unchanged. This strategy only proposes.

The buy-the-dip-in-an-uptrend idea is the same family as H4, which did not qualify on walk-forward. D1 is
untested: no backtest, paper or live result exists for it.

EMAs are computed over a fixed window (3 x the period, seeded with the simple average of its first
`period` values), so the same candles always give the same value, whatever history the caller holds.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.core.money import ZERO, D, pct
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade, entry_limit

SLOPE_DAYS = 5                   # fixed: the daily EMA must be higher than this many days earlier
EMA_WINDOW = 3                   # fixed: an EMA is computed over this many periods of candles
PULLBACK_CANDLES = 6             # fixed: the pullback must be within this many 4h candles before the signal
ATR_N = 14                       # fixed (4h)
TAKE_PROFIT_R = Decimal("2")     # fixed: take-profit 2x the stop distance (this strategy's own rule)
MAX_RANGE_ATR = Decimal("2")     # fixed: a signal candle wider than this many ATRs is a spike: no trade
MAX_STOP_PCT = Decimal("3")      # fixed: no trade if the stop is further than this below the entry
HORIZON_HOURS = 72               # fixed: time stop
STALE_MS = 15 * 60_000           # fixed: the last 4h candle must have closed within 4h + 15 min of now


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    """EMA over the last EMA_WINDOW x period values; one value per input from the seed on (oldest first)."""
    vals = values[-EMA_WINDOW * period:]
    if period < 1 or len(vals) < period:
        return []
    k = D(2) / (period + 1)
    out = [sum(vals[:period], ZERO) / period]
    for v in vals[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


@dataclass(frozen=True)
class Verdict:
    """LONG or NO TRADE for one coin at one moment, with the reason in plain words."""
    long: bool
    reason: str
    entry: Decimal | None = None
    stop: Decimal | None = None
    take_profit: Decimal | None = None
    atr: Decimal | None = None
    candle_ms: int | None = None             # open time of the signal (last finished) 4h candle


@dataclass(frozen=True)
class DailyPullback:
    symbols: list[str]
    trend_days: int = 50                     # tunable: daily EMA period
    pullback_ema: int = 20                   # tunable: 4h EMA period the pullback must touch
    stop_atr: Decimal = Decimal("1.5")       # tunable: stop distance in 4h ATRs
    name: str = "D1"
    interval: str = "4h"

    def params(self) -> dict[str, object]:
        return {"trend_days": self.trend_days, "pullback_ema": self.pullback_ema, "stop_atr": self.stop_atr}

    def with_params(self, **p: object) -> "DailyPullback":
        return replace(self, **p)

    def history_needed(self) -> int:
        days = EMA_WINDOW * self.trend_days + SLOPE_DAYS + 3      # + the day still running and 2 spare days for gaps
        return max(days * 6, EMA_WINDOW * self.pullback_ema + PULLBACK_CANDLES + 6, ATR_N * 3 + 1)

    # ------------------------------------------------------------ the rules
    def uptrend(self, days: list[Candle]) -> tuple[bool, str]:
        e = ema([d.close for d in days], self.trend_days)
        if len(e) < SLOPE_DAYS + 1:
            return False, f"not enough daily history for the {self.trend_days}-day EMA"
        last, before = e[-1], e[-1 - SLOPE_DAYS]
        if days[-1].close <= last:
            return False, f"daily close {days[-1].close} is not above the {self.trend_days}-day EMA {last:.2f}"
        if last <= before:
            return False, (f"the {self.trend_days}-day EMA is not rising "
                           f"({last:.2f} vs {before:.2f} {SLOPE_DAYS} days ago)")
        return True, f"daily close {days[-1].close} above the {self.trend_days}-day EMA {last:.2f} (rising)"

    def _trigger(self, cs: list[Candle], i: int) -> str:
        """Why candle i is a pullback-and-resume signal, or '' if it is not."""
        if i < 1 or cs[i].close <= cs[i - 1].high:
            return ""
        e = ema([c.close for c in cs[:i]], self.pullback_ema)          # EMA up to each earlier candle
        lows = cs[i - PULLBACK_CANDLES:i]
        if len(e) < PULLBACK_CANDLES or len(lows) < PULLBACK_CANDLES:
            return ""
        for c, level in zip(lows, e[-PULLBACK_CANDLES:]):
            if c.low <= level:
                return (f"4h low {c.low} touched the {self.pullback_ema}-EMA {level:.2f}; then a 4h close "
                        f"{cs[i].close} above the previous high {cs[i - 1].high}")
        return ""

    def signal(self, cs: list[Candle], now: float) -> Verdict:
        """The verdict on finished 4h candles `cs` (oldest first) at time `now`."""
        if len(cs) < self.history_needed() - 6:
            return Verdict(False, "not enough 4h history")
        last = cs[-1]
        if int(now * 1000) - last.close_time > INTERVAL_MS[self.interval] + STALE_MS:
            return Verdict(False, "price data is stale (the last 4h candle closed too long ago)")
        days = daily(cs)
        up, why_trend = self.uptrend(days)
        if not up:
            return Verdict(False, why_trend)
        why = self._trigger(cs, len(cs) - 1)
        if not why:
            return Verdict(False, f"{why_trend}; no pullback-and-resume signal on the last 4h candle")
        day = last.open_time - last.open_time % DAY_MS
        for j in range(len(cs) - 2, 0, -1):                              # at most one signal per coin per day
            if cs[j].open_time < day:
                break
            if self._trigger(cs, j):
                return Verdict(False, "already signalled earlier today (at most one signal per coin per day)")
        a = atr(cs[-ATR_N * 3:], ATR_N)[-1]
        if a is None or a <= 0:
            return Verdict(False, "no valid 4h ATR")
        if last.high - last.low > MAX_RANGE_ATR * a:
            return Verdict(False, f"the signal candle's range {last.high - last.low} is more than "
                                  f"{MAX_RANGE_ATR} x ATR {a:.2f} (a spike)", atr=a)
        entry = entry_limit(last.close)
        stop = entry - self.stop_atr * a
        if stop <= 0 or (entry - stop) / entry > pct(MAX_STOP_PCT):
            return Verdict(False, f"the stop would be more than {MAX_STOP_PCT}% below the entry", entry, stop, atr=a)
        return Verdict(True, f"{why_trend}; {why}", entry, stop, entry + TAKE_PROFIT_R * (entry - stop), a,
                       last.open_time)

    # ------------------------------------------------------------ the strategy contract
    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        a = atr(candles[-ATR_N * 3:], ATR_N)[-1] if len(candles) >= ATR_N else None
        return None if a is None or a <= 0 else (entry - self.stop_atr * a, HORIZON_HOURS)

    def intent(self, symbol: str, v: Verdict) -> TradeIntent | None:
        if not v.long or v.entry is None or v.stop is None or v.take_profit is None or not 0 < v.stop < v.entry:
            return None
        return TradeIntent(symbol=symbol, side="BUY", entry_price=v.entry, stop_price=v.stop,
                           take_profit_price=v.take_profit, strategy=self.name, horizon_hours=HORIZON_HOURS,
                           invalidation=f"the daily close falls below its {self.trend_days}-day EMA")

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            i = self.intent(s, self.signal(data.closed_candles(s, self.interval, limit=self.history_needed()), now))
            if i is not None:
                out.append(i)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        """At the first 4h close after 00:00 UTC: sell at market when the daily close is below its EMA."""
        if int(now * 1000) % DAY_MS >= INTERVAL_MS[self.interval]:
            return []
        out: list[Action] = []
        for t in open_trades:
            days = daily(data.closed_candles(t.symbol, self.interval, limit=self.history_needed()))
            e = ema([d.close for d in days], self.trend_days)
            if e and days[-1].close < e[-1]:
                out.append(Exit(t.symbol, f"the daily close fell below its {self.trend_days}-day EMA"))
        return out
