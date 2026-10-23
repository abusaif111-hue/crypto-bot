"""Technical indicators (module 4). Pure functions, Decimal in and out, no I/O.

Every function returns a list aligned with its input: value i uses only inputs
0..i, never anything later (no look-ahead; tested by changing later inputs and
checking earlier values do not move). Positions without enough history are None.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from bot.core.money import ZERO, D
from bot.exchange.interface import Candle

Series = list[Decimal | None]


def closes(candles: Sequence[Candle]) -> list[Decimal]:
    return [c.close for c in candles]


def sma(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    out: Series = []
    total = ZERO
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def highest(values: Sequence[Decimal], n: int) -> Series:
    """Highest of the last n values, the current one included."""
    _check(n)
    return [max(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def lowest(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    return [min(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def true_range(candles: Sequence[Candle]) -> list[Decimal]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], n: int = 14) -> Series:
    """Average true range, Wilder's smoothing: the first value is the plain mean of n true ranges."""
    _check(n)
    tr = true_range(candles)
    out: Series = []
    prev: Decimal | None = None
    for i, t in enumerate(tr):
        if i < n - 1:
            out.append(None)
            continue
        prev = sum(tr[:n], ZERO) / n if prev is None else (prev * (n - 1) + t) / n
        out.append(prev)
    return out


def pct_change(values: Sequence[Decimal], n: int) -> Series:
    """Return over the last n steps, as a fraction (0.05 = +5%)."""
    _check(n)
    return [values[i] / values[i - n] - 1 if i >= n and values[i - n] > 0 else None for i in range(len(values))]


def realized_vol(values: Sequence[Decimal], n: int) -> Series:
    """Standard deviation of the last n log returns (per step, not annualised)."""
    _check(n, minimum=2)
    logs: list[Decimal | None] = [None] + [
        (values[i] / values[i - 1]).ln() if values[i - 1] > 0 and values[i] > 0 else None
        for i in range(1, len(values))]
    out: Series = []
    for i in range(len(values)):
        window = logs[i - n + 1:i + 1] if i >= n else []
        if len(window) < n or any(x is None for x in window):
            out.append(None)
            continue
        mean = sum(window, ZERO) / n
        var = sum(((x - mean) ** 2 for x in window), ZERO) / (n - 1)
        out.append(var.sqrt())
    return out


def rsi(values: Sequence[Decimal], n: int = 14) -> Series:
    """Relative strength index (Wilder), 0-100."""
    _check(n)
    out: Series = [None] * len(values)
    gains = losses = ZERO
    avg_g = avg_l = None
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        g, lo = max(ch, ZERO), max(-ch, ZERO)
        if i <= n:
            gains += g
            losses += lo
            if i == n:
                avg_g, avg_l = gains / n, losses / n
        else:
            avg_g = (avg_g * (n - 1) + g) / n
            avg_l = (avg_l * (n - 1) + lo) / n
        if avg_g is not None:
            out[i] = D(100) if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def zscore(values: Sequence[Decimal], n: int) -> Series:
    """How many standard deviations the value is from its n-step mean."""
    _check(n, minimum=2)
    out: Series = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
            continue
        w = values[i - n + 1:i + 1]
        mean = sum(w, ZERO) / n
        sd = (sum(((x - mean) ** 2 for x in w), ZERO) / (n - 1)).sqrt()
        out.append((values[i] - mean) / sd if sd > 0 else ZERO)
    return out


def _check(n: int, minimum: int = 1) -> None:
    if not isinstance(n, int) or n < minimum:
        raise ValueError(f"window must be a whole number of at least {minimum}")


def percentile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    """Linear-interpolated percentile, q in [0, 1]."""
    if not values:
        raise ValueError("no values")
    v = sorted(values)
    pos = (len(v) - 1) * D(q)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)
