"""Market regime labels (module 4; the rules were fixed before any strategy was tested). Pure, no I/O.

Used by the backtests' results by regime (`bot.backtest.selection`) and by the daily market view
(`bot.intel.market`), so both use exactly the same definition.
"""
from __future__ import annotations

from bot.analysis.indicators import percentile, realized_vol, sma
from bot.analysis.resample import daily
from bot.core.money import D
from bot.exchange.interface import Candle


def regime_labels(candles: list[Candle]) -> dict[int, tuple[str, bool]]:
    """Per day: bull / bear / sideways from the 200-day average and its 20-day slope; high-volatility
    when the 30-day volatility is in the top fifth of the research period."""
    days = daily(candles)
    closes = [d.close for d in days]
    avg = sma(closes, 200)
    vol = realized_vol(closes, 30)
    known = sorted(v for v in vol if v is not None)
    cut = percentile(known, D("0.8")) if known else None
    out = {}
    for i, d in enumerate(days):
        a, a20 = avg[i], avg[i - 20] if i >= 20 else None
        if a is None or a20 is None:
            label = "unlabelled"
        elif d.close > a and a > a20:
            label = "bull"
        elif d.close < a and a < a20:
            label = "bear"
        else:
            label = "sideways"
        out[d.open_time] = (label, cut is not None and vol[i] is not None and vol[i] >= cut)
    return out
