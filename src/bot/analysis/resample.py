"""Daily candles from finished 4-hour candles (module 4), and hourly candles from finished 15-minute
candles (intraday research, docs/intraday-plan.md). Pure, no I/O.

Only complete periods are returned: a day counts once its last candle has closed at midnight (an hour
once its last candle has closed on the hour), so a strategy never sees a period that is still running.
Binance's maintenance periods (shorter candles, gaps) still count when they ran to the period's end.
"""
from __future__ import annotations

from typing import Sequence

from bot.core.money import ZERO
from bot.exchange.interface import Candle

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def daily(candles: Sequence[Candle]) -> list[Candle]:
    out: list[Candle] = []
    day_start = None
    group: list[Candle] = []

    def flush() -> None:
        if group and group[-1].close_time + 1 == day_start + DAY_MS:
            out.append(Candle(open_time=day_start, open=group[0].open, high=max(c.high for c in group),
                              low=min(c.low for c in group), close=group[-1].close,
                              volume=sum((c.volume for c in group), ZERO), close_time=day_start + DAY_MS - 1,
                              quote_volume=sum((c.quote_volume for c in group), ZERO), closed=True))

    for c in candles:
        d = c.open_time - c.open_time % DAY_MS
        if d != day_start:
            flush()
            day_start, group = d, []
        group.append(c)
    flush()
    return out


def resample(candles: Sequence[Candle], period_ms: int) -> list[Candle]:
    """Finished candles of `period_ms` (whole hours or days, UTC) from shorter finished candles: the same
    rule as `daily`, for any period (`resample(cs, DAY_MS) == daily(cs)`, tested)."""
    out: list[Candle] = []
    start = None
    group: list[Candle] = []

    def flush() -> None:
        if group and group[-1].close_time + 1 == start + period_ms:
            out.append(Candle(open_time=start, open=group[0].open, high=max(c.high for c in group),
                              low=min(c.low for c in group), close=group[-1].close,
                              volume=sum((c.volume for c in group), ZERO), close_time=start + period_ms - 1,
                              quote_volume=sum((c.quote_volume for c in group), ZERO), closed=True))

    for c in candles:
        p = c.open_time - c.open_time % period_ms
        if p != start:
            flush()
            start, group = p, []
        group.append(c)
    flush()
    return out


def hourly(candles: Sequence[Candle]) -> list[Candle]:
    """Finished 1-hour candles from finished 15-minute candles (the intraday research's context)."""
    return resample(candles, HOUR_MS)
