"""How close H2b is to buying, coin by coin (read-only; asked for by Sultan, 24 Oct 2026).

For each coin it shows the three things H2b checks once a day (docs/h2b-plan.md): each lookback's
return against the bar it must clear (`threshold` x 30-day volatility x sqrt(days)), the volatility
pause, and H2b's own verdict, which comes from `BlendedMomentum.signal()` itself, so this report can
never disagree with what the bot does. It also shows the exit vote for an open trade (sells when 2
of the 3 returns are below zero).

It changes nothing: it does not touch H2b's code (the forward test fingerprints it), places no
order, writes no file, and reads only public market data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from bot.analysis.indicators import percentile
from bot.core.money import D
from bot.strategies.base import CandleSource, entry_limit
from bot.strategies.h2b_blend import (AGREE, LOOKBACK_DAYS, PAUSE_DAYS, PAUSE_PERCENTILE, VOL_DAYS,
                                      BlendedMomentum)


@dataclass(frozen=True)
class Lookback:
    days: int
    ret: Decimal
    needed: Decimal
    strong: bool


@dataclass(frozen=True)
class CoinSignal:
    symbol: str
    enter: bool
    why: str                                   # H2b's own reason when it would not buy
    last_day_ms: int | None = None             # open time of the last finished daily candle
    lookbacks: list[Lookback] = field(default_factory=list)
    vol: Decimal | None = None                 # 30-day volatility, per day
    pause_level: Decimal | None = None         # the 80th percentile of the past year's 30-day volatility
    paused: bool | None = None
    below_zero: int | None = None              # returns below zero (exit vote for an open trade)
    stop_pct: Decimal | None = None            # stop distance below the entry, when it would buy
    error: str = ""


def coin_signal(strategy: BlendedMomentum, data: CandleSource, symbol: str) -> CoinSignal:
    try:
        cs = data.closed_candles(symbol, strategy.interval, limit=strategy.history_needed())
    except Exception as e:                                  # market data down: say so, show nothing
        return CoinSignal(symbol, False, "", error=f"{type(e).__name__}: {e}")
    days = strategy._daily(cs)
    enter, why = strategy.signal(days)                      # the verdict is H2b's own
    out = {"last_day_ms": days[-1].open_time if days else None}
    rets = strategy._returns(days)
    vol = strategy._vol(days, len(days) - 1) if days else None
    if rets is not None and vol is not None and vol > 0:
        out["vol"] = vol
        out["lookbacks"] = [Lookback(n, r, strategy.threshold * vol * D(n).sqrt(),
                                     r > strategy.threshold * vol * D(n).sqrt()) for r, n in zip(rets, LOOKBACK_DAYS)]
        out["below_zero"] = sum(1 for r in rets if r < 0)
        today = len(days) - 1
        if len(days) >= VOL_DAYS + PAUSE_DAYS + 1:
            earlier = [strategy._vol(days, i) for i in range(today - PAUSE_DAYS, today)]
            if all(v is not None for v in earlier):
                out["pause_level"] = percentile(earlier, PAUSE_PERCENTILE)
                out["paused"] = vol >= out["pause_level"]
    if enter and cs:
        entry = entry_limit(cs[-1].close)
        plan = strategy.exit_plan(cs, entry)
        if plan:
            out["stop_pct"] = (entry - plan[0]) / entry * 100
    return CoinSignal(symbol, enter, why, **out)


def report(strategy: BlendedMomentum, data: CandleSource) -> list[CoinSignal]:
    return [coin_signal(strategy, data, s) for s in strategy.symbols]


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.2f}%"


def render(signals: list[CoinSignal], strategy: BlendedMomentum, warning: str = "") -> str:
    lines = [f"H2b signal check (read-only: public live prices; changes nothing). Settings: threshold "
             f"{strategy.threshold}, stop {strategy.atr_mult} x daily ATR.",
             "H2b decides once a day, after the 00:00 UTC daily close (04:00 Muscat), on finished days only."]
    if warning:
        lines.append(f"WARNING: {warning}")
    for s in signals:
        lines.append("")
        if s.error:
            lines.append(f"{s.symbol:<9} could not read prices: {s.error}")
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(s.last_day_ms / 1000)) if s.last_day_ms else "?"
        if s.enter:
            stop = f"; stop about {s.stop_pct:.1f}% below the entry" if s.stop_pct is not None else ""
            lines.append(f"{s.symbol:<9} BUY CONDITIONS MET on {day}{stop}. The risk engine still decides, "
                         f"and nothing is bought if the coin is already held.")
        else:
            lines.append(f"{s.symbol:<9} no entry ({day}): {s.why}")
        for lb in s.lookbacks:
            lines.append(f"  {lb.days:>2} days  {_pct(lb.ret):>8}   needs above {_pct(lb.needed):>8}   "
                         f"{'strong' if lb.strong else 'not yet'}")
        if s.lookbacks:
            lines.append(f"  {sum(lb.strong for lb in s.lookbacks)} of {len(s.lookbacks)} strong (H2b needs {AGREE})")
        if s.vol is not None:
            pause = ("unknown (needs a year of history)" if s.paused is None else
                     f"{'ON: no new entries' if s.paused else 'off'} (pause from {s.pause_level * 100:.2f}% a day)")
            lines.append(f"  volatility {s.vol * 100:.2f}% a day; volatility pause {pause}")
        if s.below_zero is not None:
            lines.append(f"  exit vote for an open trade: {s.below_zero} of 3 returns below zero "
                         f"(sells at {AGREE})")
    return "\n".join(lines) + "\n"
