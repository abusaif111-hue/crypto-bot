"""Portfolio accounting: equity, P&L periods and the drawdown index.

Definitions follow section 4 "Measurement definitions" of the design package:
- Equity = quote balance + every other asset (free + locked) valued at best bid.
- Deposits and withdrawals are external flows: excluded from P&L.
- Daily P&L = equity now - equity at day start - flows today. Weekly the same.
- Drawdown uses a unit-value index (like a fund NAV), so flows don't move it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.core.money import ONE, ZERO, D


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class MissingPrice(ValueError):
    pass


def value_equity(balances: list[Balance], bids: dict[str, Decimal], quote: str = "USDT") -> Decimal:
    """Value everything in the quote asset at the best bid. A missing price is an error, never zero."""
    total = ZERO
    for b in balances:
        if b.total == 0:
            continue
        if b.asset == quote:
            total += b.total
            continue
        bid = bids.get(b.asset + quote)
        if bid is None or bid <= 0:
            raise MissingPrice(f"no bid for {b.asset}{quote}; equity cannot be valued")
        total += b.total * bid
    return total


@dataclass
class EquityIndex:
    """Unit-value index. Deposits buy units at the current value; withdrawals redeem them."""
    units: Decimal = ZERO
    value_per_unit: Decimal = ONE
    peak_value_per_unit: Decimal = ONE

    def start(self, equity: Decimal) -> None:
        if equity <= 0:
            raise ValueError("starting equity must be positive")
        self.units, self.value_per_unit, self.peak_value_per_unit = D(equity), ONE, ONE

    def mark(self, equity: Decimal) -> None:
        if self.units <= 0:
            raise RuntimeError("index not started")
        self.value_per_unit = D(equity) / self.units
        self.peak_value_per_unit = max(self.peak_value_per_unit, self.value_per_unit)

    def flow(self, amount: Decimal, equity_before_flow: Decimal) -> None:
        """Record a deposit (+) or withdrawal (-) at the value just before it arrived."""
        self.mark(equity_before_flow)
        self.units += D(amount) / self.value_per_unit
        if self.units <= 0:
            raise ValueError("withdrawal larger than the account")

    @property
    def drawdown_pct(self) -> Decimal:
        # 6 decimal places of a percent is far finer than any limit and removes division residue
        return ((1 - self.value_per_unit / self.peak_value_per_unit) * 100).quantize(Decimal("0.000001"))


def day_start(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(t: datetime) -> datetime:
    d = day_start(t)
    return d - timedelta(days=d.weekday())  # Monday 00:00 UTC


@dataclass
class PeriodTracker:
    """Tracks day-start and week-start equity and the flows inside each period."""
    day_anchor: datetime | None = None
    week_anchor: datetime | None = None
    day_start_equity: Decimal = ZERO
    week_start_equity: Decimal = ZERO
    day_flows: Decimal = ZERO
    week_flows: Decimal = ZERO
    rolled: list[str] = field(default_factory=list)

    def update(self, now: datetime, equity: Decimal) -> list[str]:
        """Call with every equity mark. Returns ['day'] / ['week'] when a period rolls over."""
        events = []
        ds, ws = day_start(now), week_start(now)
        if self.day_anchor != ds:
            self.day_anchor, self.day_start_equity, self.day_flows = ds, D(equity), ZERO
            events.append("day")
        if self.week_anchor != ws:
            self.week_anchor, self.week_start_equity, self.week_flows = ws, D(equity), ZERO
            events.append("week")
        return events

    def flow(self, amount: Decimal) -> None:
        self.day_flows += D(amount)
        self.week_flows += D(amount)

    def daily_pnl(self, equity: Decimal) -> Decimal:
        return D(equity) - self.day_start_equity - self.day_flows

    def weekly_pnl(self, equity: Decimal) -> Decimal:
        return D(equity) - self.week_start_equity - self.week_flows
