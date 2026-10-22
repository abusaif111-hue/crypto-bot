"""Exact decimal helpers. Money and quantities never use floats."""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, getcontext

getcontext().prec = 40

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def D(value: object) -> Decimal:
    """Convert to Decimal via str, so 0.1 stays 0.1 rather than its float expansion."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def pct(value: Decimal) -> Decimal:
    """Percent (0.25) to fraction (0.0025)."""
    return D(value) / HUNDRED


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of step. Used for quantities: never round size up."""
    value, step = D(value), D(step)
    if step <= 0:
        raise ValueError("step must be positive")
    if value <= 0:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def floor_price(value: Decimal, tick: Decimal) -> Decimal:
    return floor_to_step(value, tick)


def ceil_price(value: Decimal, tick: Decimal) -> Decimal:
    value, tick = D(value), D(tick)
    if tick <= 0:
        raise ValueError("tick must be positive")
    return (value / tick).to_integral_value(rounding=ROUND_UP) * tick


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return D(step) > 0 and D(value) % D(step) == 0


def fmt(value: object) -> str:
    """Short form for people to read (logs, status). Never used in calculations.

    1 and above: 2 decimals (81754.87). Below 1: 6 significant digits (0.0096, 0.784847).
    """
    v = D(value)
    if v.is_nan() or v.is_infinite():
        return str(v)
    if abs(v) >= ONE or v == 0:
        return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return f"{v.normalize():.6g}"
