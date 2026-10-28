"""H1-DailyTrader: intraday setups on 1h candles with 4h context (Sultan's decision, 28 Oct 2026).

A new strategy, separate from D1 and H2b. Three setups, each its own strategy with at most 3 tunable
settings (design package), evaluated separately:
- BRK, breakout: the last 4h close is above the 4h 20-EMA; the last 1h close breaks above the highest high
  of the previous `lookback` 1h candles, and the close before it did not (only a fresh break counts);
- CONT, trend continuation: the 4h 20-EMA is rising (above its value 3 candles earlier) with the 4h close
  above it; the 1h close is above a rising 1h EMA(`lookback`) and breaks above the highest high of the
  previous 3 hours (a fresh break);
- RNG, range: the 4h 20-EMA is flat (moved less than one 4h ATR over 6 candles); over the previous
  `lookback` hours the range is at least 4 x the 1h ATR wide; the last 1h candle dipped into the bottom
  quarter of the range and closed up, inside it. The stop is below the range low; the target must lie
  inside the range.
There is no 50-day filter and no shorting (Spot, long-only).

Stops and targets: BRK and CONT stop `stop_atr` x 1h ATR(14) below the entry; RNG `stop_atr` x ATR below the
range low. Target `target_r` x the stop distance. Entry limit at the last close + 0.10% (as every strategy;
a higher open does not fill: no chasing); time stop 24 hours. The risk engine sizes every idea from the
actual stop distance and refuses any whose reward-to-risk after its slippage and fees is below 1.5.

The 15-minute entry confirmation in Sultan's brief is not part of this strategy: 15m history does not fit
the 512 MB server (measured, 25 Oct 2026), and a rule that cannot be backtested is not traded. The
pullback setup is left out: hourly pullbacks in an uptrend are H4, rejected on 24 Oct 2026, whose rules
forbid re-testing it with a trend filter.

Untested: no backtest, paper or live result exists for any setup yet.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr
from bot.analysis.resample import resample
from bot.core.money import ZERO, D
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, OpenTrade, entry_limit

SETUPS = ("BRK", "CONT", "RNG")
H4_MS = 4 * 3_600_000
CTX_EMA = 20                     # fixed: 4h EMA period for the context
CTX_SLOPE = 3                    # fixed: CONT's 4h EMA must be above its value this many 4h candles earlier
FLAT_CANDLES = 6                 # fixed: RNG's 4h EMA must move less than one 4h ATR over this many candles
CONT_BREAK = 3                   # fixed: CONT breaks the highest high of this many previous hours
CONT_SLOPE = 5                   # fixed: CONT's 1h EMA must be above its value this many hours earlier
RANGE_MIN_ATR = Decimal("4")     # fixed: RNG's range is at least this many 1h ATRs wide
RANGE_ZONE = Decimal("0.25")     # fixed: RNG's dip reaches the bottom quarter of the range
ATR_N = 14                       # fixed (1h and 4h)
EMA_WINDOW = 3                   # fixed: an EMA is computed over this many periods of candles
HORIZON_HOURS = 24               # fixed: time stop
DEFAULTS = {"BRK": 24, "CONT": 50, "RNG": 48}   # `lookback` per setup


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    """EMA over the last EMA_WINDOW x period values, seeded with the simple average of the first `period`;
    the same candles always give the same value, whatever history the caller holds."""
    vals = values[-EMA_WINDOW * period:]
    if period < 1 or len(vals) < period:
        return []
    k = D(2) / (period + 1)
    out = [sum(vals[:period], ZERO) / period]
    for v in vals[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


@dataclass(frozen=True)
class Setup:
    """A setup found on the last finished 1h candle: the plan, and the level that defines it."""
    entry: Decimal
    stop: Decimal
    target: Decimal
    level: Decimal               # BRK/CONT: the high broken; RNG: the range low
    reason: str
    invalidation: str


@dataclass(frozen=True)
class DailyTrader:
    symbols: list[str]
    setup: str = "BRK"
    lookback: int = 0                        # tunable: BRK/RNG hours of range, CONT the 1h EMA period (0: default)
    stop_atr: Decimal = Decimal("1.5")       # tunable: stop distance in 1h ATRs
    target_r: Decimal = Decimal("3")         # tunable: target in multiples of the stop distance
    interval: str = "1h"

    def __post_init__(self) -> None:
        if self.setup not in SETUPS:
            raise ValueError(f"setup must be one of {SETUPS}")
        if self.lookback == 0:
            object.__setattr__(self, "lookback", DEFAULTS[self.setup])
        if self.lookback < 2 or self.stop_atr <= 0 or self.target_r <= 0:
            raise ValueError("lookback must be at least 2; stop_atr and target_r positive")

    @property
    def name(self) -> str:
        return f"H1DT-{self.setup}"

    def params(self) -> dict[str, object]:
        return {"lookback": self.lookback, "stop_atr": self.stop_atr, "target_r": self.target_r}

    def with_params(self, **p: object) -> "DailyTrader":
        return replace(self, **p)

    def history_needed(self) -> int:
        ctx = (EMA_WINDOW * CTX_EMA + FLAT_CANDLES + 2) * 4             # 1h candles behind the 4h context
        own = EMA_WINDOW * self.lookback + CONT_SLOPE + 2 if self.setup == "CONT" else self.lookback + 2
        return max(ctx, own, ATR_N * 3 + 1) + 4

    # ------------------------------------------------------------ the 4h context
    def context(self, hours: list[Candle]) -> tuple[bool, str]:
        fours = resample(hours, H4_MS)
        e = ema([c.close for c in fours], CTX_EMA)
        if len(e) < FLAT_CANDLES + 1:
            return False, "not enough 4h history"
        last = fours[-1].close
        if self.setup == "BRK":
            ok = last > e[-1]
            return ok, f"4h close {last} {'above' if ok else 'not above'} the 4h {CTX_EMA}-EMA {e[-1]:.2f}"
        if self.setup == "CONT":
            ok = last > e[-1] > e[-1 - CTX_SLOPE]
            return ok, (f"4h close {last} vs 4h {CTX_EMA}-EMA {e[-1]:.2f} ({CTX_SLOPE} candles ago "
                        f"{e[-1 - CTX_SLOPE]:.2f}): {'rising, price above' if ok else 'not a rising trend'}")
        a = atr(fours[-ATR_N * 3:], ATR_N)[-1]
        if a is None or a <= 0:
            return False, "no valid 4h ATR"
        move = abs(e[-1] - e[-1 - FLAT_CANDLES])
        ok = move < a
        return ok, (f"4h {CTX_EMA}-EMA moved {move:.2f} over {FLAT_CANDLES} candles, "
                    f"{'under' if ok else 'not under'} one 4h ATR {a:.2f} ({'flat' if ok else 'trending'})")

    # ------------------------------------------------------------ the 1h trigger
    def _atr(self, hours: list[Candle]) -> Decimal | None:
        a = atr(hours[-ATR_N * 3:], ATR_N)[-1] if len(hours) > ATR_N else None
        return a if a is not None and a > 0 else None

    def trigger(self, hours: list[Candle]) -> Setup | None:
        """The setup on the last finished 1h candle, ignoring the 4h context; None if there is none."""
        if len(hours) < self.history_needed() - 4:
            return None
        a = self._atr(hours)
        if a is None:
            return None
        last, prev = hours[-1], hours[-2]
        entry = entry_limit(last.close)
        if self.setup in ("BRK", "CONT"):
            n = self.lookback if self.setup == "BRK" else CONT_BREAK
            level = max(c.high for c in hours[-1 - n:-1])
            level_before = max(c.high for c in hours[-2 - n:-2])
            if not (last.close > level and prev.close <= level_before):
                return None
            if self.setup == "CONT":
                e = ema([c.close for c in hours], self.lookback)
                if len(e) < CONT_SLOPE + 1 or not (last.close > e[-1] > e[-1 - CONT_SLOPE]):
                    return None
            stop = entry - self.stop_atr * a
            why = (f"1h close {last.close} broke the {n}-hour high {level}" +
                   (f", above a rising 1h {self.lookback}-EMA" if self.setup == "CONT" else ""))
            invalid = f"a 1h close back below {level} before the entry"
        else:
            window = hours[-1 - self.lookback:-1]
            lo, hi = min(c.low for c in window), max(c.high for c in window)
            if hi - lo < RANGE_MIN_ATR * a:
                return None
            zone = lo + RANGE_ZONE * (hi - lo)
            if not (last.low <= zone and last.close > last.open and lo < last.close < hi):
                return None
            stop = lo - self.stop_atr * a
            level = lo
            why = (f"1h candle dipped to {last.low} (bottom quarter of the {self.lookback}-hour range "
                   f"{lo} to {hi}) and closed up at {last.close}")
            invalid = f"a 1h close below the range low {lo}"
        if not 0 < stop < entry:
            return None
        target = entry + self.target_r * (entry - stop)
        if self.setup == "RNG" and target > max(c.high for c in hours[-1 - self.lookback:-1]):
            return None                                                  # the target must lie inside the range
        return Setup(entry, stop, target, level, why, invalid)

    # ------------------------------------------------------------ the strategy contract
    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        a = self._atr(candles)
        return None if a is None else (entry - self.stop_atr * a, HORIZON_HOURS)

    def take_profit(self, entry: Decimal, stop: Decimal) -> Decimal:
        """The target for an entry and stop (random entries with the same exits use it too)."""
        return entry + self.target_r * (entry - stop)

    def intent(self, symbol: str, s: Setup) -> TradeIntent:
        return TradeIntent(symbol=symbol, side="BUY", entry_price=s.entry, stop_price=s.stop,
                           take_profit_price=s.target, strategy=self.name, horizon_hours=HORIZON_HOURS,
                           invalidation=s.invalidation)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for sym in self.symbols:
            hours = data.closed_candles(sym, self.interval, limit=self.history_needed())
            s = self.trigger(hours)                                      # the cheap 1h check first
            if s is not None and self.context(hours)[0]:
                out.append(self.intent(sym, s))
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        return []                                                        # stop, target and the 24h time stop only
