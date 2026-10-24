"""Daily market observations (docs/intel-plan.md, step 2; approved by Sultan on 24 Oct 2026).

For each coin, from finished candles only: the last daily close and its change over 1 and 7 days, the
trend against the 50-day average (and whether that average is rising), the regime label the backtests
use (200-day average and its 20-day slope, `bot.analysis.regime`), the 30-day volatility and where it
stands in the coin's own past year, the last day's volume against its 30-day average, and the spread.
Breadth is the share of the observed coins (the forward test's four, fixed by the coin screen before
any data was seen) above their 50-day average.

These are observations, not forecasts: nothing here predicts a price or says what to buy. Missing or
short history is reported as such, never filled in. Read-only: it imports nothing that trades.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot.analysis.indicators import realized_vol, sma
from bot.analysis.regime import regime_labels
from bot.analysis.resample import daily
from bot.exchange.interface import INTERVAL_MS, Candle

INTERVAL = "4h"
TREND_DAYS, SLOPE_DAYS = 50, 10
VOL_DAYS, VOL_YEAR = 30, 365
VOLUME_DAYS = 30
HISTORY_DAYS = VOL_DAYS + VOL_YEAR + 1 + 3          # enough for every figure (the 200-day regime needs 221)
CANDLES_NEEDED = HISTORY_DAYS * (86_400_000 // INTERVAL_MS[INTERVAL])


@dataclass(frozen=True)
class Observation:
    symbol: str
    day: str | None = None                      # the last finished UTC day
    close: Decimal | None = None
    change_1d_pct: Decimal | None = None
    change_7d_pct: Decimal | None = None
    above_avg50: bool | None = None
    from_avg50_pct: Decimal | None = None
    avg50_rising: bool | None = None
    regime: str | None = None                   # bull, bear, sideways or unlabelled (backtest rule)
    vol30_pct: Decimal | None = None            # daily volatility, in %
    vol_rank_pct: Decimal | None = None         # share of the past year's 30-day volatility below today's
    volume_vs_avg: Decimal | None = None        # last day's quote volume / its 30-day average
    spread_pct: Decimal | None = None
    notes: list[str] = field(default_factory=list)
    error: str = ""


def _q(x: Decimal, places: str = "0.01") -> Decimal:
    return x.quantize(Decimal(places))


def observe(symbol: str, candles: list[Candle], bid: Decimal | None = None, ask: Decimal | None = None) -> Observation:
    """One coin's observations from its finished candles (any interval; daily candles are built from them)."""
    days = daily(candles)
    if len(days) < 2:
        return Observation(symbol, notes=["not enough finished days"])
    closes = [d.close for d in days]
    out: dict[str, Any] = {"day": time.strftime("%Y-%m-%d", time.gmtime(days[-1].open_time / 1000)),
                           "close": closes[-1], "change_1d_pct": _q((closes[-1] / closes[-2] - 1) * 100)}
    notes: list[str] = []
    if len(closes) > 7:
        out["change_7d_pct"] = _q((closes[-1] / closes[-8] - 1) * 100)
    avg = sma(closes, TREND_DAYS) if len(closes) >= TREND_DAYS else []
    if avg and avg[-1] is not None:
        out["above_avg50"] = closes[-1] > avg[-1]
        out["from_avg50_pct"] = _q((closes[-1] / avg[-1] - 1) * 100)
        if len(avg) > SLOPE_DAYS and avg[-1 - SLOPE_DAYS] is not None:
            out["avg50_rising"] = avg[-1] > avg[-1 - SLOPE_DAYS]
    else:
        notes.append(f"fewer than {TREND_DAYS} days: no trend")
    out["regime"] = regime_labels(candles).get(days[-1].open_time, ("unlabelled", False))[0]
    vols = realized_vol(closes, VOL_DAYS) if len(closes) > VOL_DAYS else []
    if vols and vols[-1] is not None:
        out["vol30_pct"] = _q(vols[-1] * 100)
        earlier = [v for v in vols[-(VOL_YEAR + 1):-1] if v is not None]
        if len(earlier) >= VOL_YEAR:
            out["vol_rank_pct"] = _q(Decimal(sum(1 for v in earlier if v < vols[-1])) / len(earlier) * 100, "0.1")
        else:
            notes.append("less than a year of volatility history: no rank")
    else:
        notes.append(f"fewer than {VOL_DAYS + 1} days: no volatility")
    if len(days) > VOLUME_DAYS:
        base = sum((d.quote_volume for d in days[-VOLUME_DAYS - 1:-1]), Decimal(0)) / VOLUME_DAYS
        if base > 0:
            out["volume_vs_avg"] = _q(days[-1].quote_volume / base)
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        out["spread_pct"] = _q((ask - bid) / ((ask + bid) / 2) * 100, "0.0001")
    return Observation(symbol, notes=notes, **out)


@dataclass(frozen=True)
class Breadth:
    above_avg50: int
    known: int
    total: int


def breadth(obs: list[Observation]) -> Breadth:
    known = [o for o in obs if o.above_avg50 is not None]
    return Breadth(sum(1 for o in known if o.above_avg50), len(known), len(obs))


def gather(data: Any, symbols: list[str]) -> list[Observation]:
    """Reads public prices through the read-only market data service; a failure is shown, never a crash."""
    try:
        books = data.books(list(symbols))
    except Exception:
        books = {}
    out = []
    for s in symbols:
        try:
            candles = data.closed_candles(s, INTERVAL, limit=CANDLES_NEEDED)
        except Exception as e:
            out.append(Observation(s, error=f"{type(e).__name__}: {e}"[:160]))
            continue
        b = books.get(s)
        out.append(observe(s, candles, getattr(b, "bid", None), getattr(b, "ask", None)))
    return out


def _p(x: Decimal | None, sign: bool = True) -> str:
    return "?" if x is None else (f"{x:+.2f}%" if sign else f"{x:.2f}%")


def render(obs: list[Observation], now: float) -> str:
    b = breadth(obs)
    lines = [f"Market observations, {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} (finished daily candles; "
             f"observations, not forecasts).",
             f"Breadth: {b.above_avg50} of {b.known} coins above their 50-day average"
             + (f" ({b.total - b.known} without enough history)" if b.known < b.total else "") + "."]
    for o in obs:
        lines.append("")
        if o.error:
            lines.append(f"{o.symbol:<9} could not read prices: {o.error}")
            continue
        lines.append(f"{o.symbol:<9} {o.day}: close {'?' if o.close is None else f'{o.close.normalize():f}'}, day {_p(o.change_1d_pct)}, week {_p(o.change_7d_pct)}; "
                     f"regime {o.regime}")
        if o.above_avg50 is not None:
            slope = "" if o.avg50_rising is None else (", average rising" if o.avg50_rising else ", average falling")
            lines.append(f"  trend: {'above' if o.above_avg50 else 'below'} its 50-day average "
                         f"({_p(o.from_avg50_pct)}{slope})")
        if o.vol30_pct is not None:
            rank = "" if o.vol_rank_pct is None else f"; higher than {o.vol_rank_pct}% of its past year"
            lines.append(f"  volatility: {_p(o.vol30_pct, sign=False)} a day over 30 days{rank}")
        if o.volume_vs_avg is not None or o.spread_pct is not None:
            vol = "" if o.volume_vs_avg is None else f"volume {o.volume_vs_avg}x its 30-day average"
            spr = "" if o.spread_pct is None else f"spread {o.spread_pct}%"
            lines.append("  " + "; ".join(x for x in (vol, spr) if x))
        for n in o.notes:
            lines.append(f"  note: {n}")
    return "\n".join(lines) + "\n"
