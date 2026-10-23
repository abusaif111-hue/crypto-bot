"""Backtest metrics (design package, "Reported metrics"). Pure, Decimal."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from bot.backtest.engine import EquityPoint
from bot.core.money import ZERO, D
from bot.paper.sim import ClosedTrade, Fill

DAY_MS = 86_400_000


@dataclass(frozen=True)
class Metrics:
    start_equity: Decimal
    end_equity: Decimal
    net_return: Decimal                 # fraction: 0.12 = +12%
    gross_return: Decimal | None        # before fees and slippage; None for benchmarks
    cagr: Decimal
    annual_vol: Decimal | None
    max_drawdown: Decimal               # fraction, positive
    max_drawdown_days: Decimal          # longest time from a peak until equity is back at it (or the end)
    days: Decimal
    exposure: Decimal                   # share of time with a position open
    trades: int | None = None
    win_rate: Decimal | None = None
    avg_win: Decimal | None = None
    avg_loss: Decimal | None = None
    profit_factor: Decimal | None = None
    expectancy: Decimal | None = None   # mean net P&L per trade, USDT
    fees: Decimal | None = None
    slippage: Decimal | None = None
    pnl_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    pnl_by_month: dict[str, Decimal] = field(default_factory=dict)


def compute(equity: list[EquityPoint], start_equity: Decimal, start_ms: int,
            trades: list[ClosedTrade] | None = None, fills: list[Fill] | None = None) -> Metrics:
    if not equity:
        raise ValueError("no equity points")
    end = equity[-1].equity
    days = D(equity[-1].at_ms - start_ms) / DAY_MS
    ratio = end / start_equity
    cagr = (ratio ** (D("365.25") / days) - 1) if days > 0 and ratio > 0 else D(-1)

    peak, peak_ms, max_dd, max_dd_ms, underwater = start_equity, start_ms, ZERO, 0, False
    for p in equity:
        if p.equity >= peak:
            if underwater:                                  # recovered: peak to recovery
                max_dd_ms = max(max_dd_ms, p.at_ms - peak_ms)
                underwater = False
            peak, peak_ms = p.equity, p.at_ms
        else:
            underwater = True
            max_dd = max(max_dd, 1 - p.equity / peak)
    if underwater:                                          # never recovered: peak to the end
        max_dd_ms = max(max_dd_ms, equity[-1].at_ms - peak_ms)

    m = dict(start_equity=start_equity, end_equity=end, net_return=ratio - 1, gross_return=None, cagr=cagr,
             annual_vol=_annual_vol(equity, start_equity), max_drawdown=max_dd, max_drawdown_days=D(max_dd_ms) / DAY_MS,
             days=days, exposure=D(sum(1 for p in equity if p.invested)) / len(equity))
    if trades is not None:
        fees = sum((f.fee for f in fills or []), ZERO)
        slip = sum((f.slippage for f in fills or []), ZERO)
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]
        by_sym, by_month = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for t in trades:
            by_sym[t.symbol] += t.pnl
            by_month[datetime.fromtimestamp(t.closed_at, timezone.utc).strftime("%Y-%m")] += t.pnl
        m.update(
            gross_return=(end + fees + slip) / start_equity - 1, trades=len(trades),
            win_rate=D(len(wins)) / len(trades) if trades else None,
            avg_win=sum(wins, ZERO) / len(wins) if wins else None,
            avg_loss=sum(losses, ZERO) / len(losses) if losses else None,
            profit_factor=(sum(wins, ZERO) / -sum(losses, ZERO)) if losses and sum(losses, ZERO) < 0 else None,
            expectancy=sum((t.pnl for t in trades), ZERO) / len(trades) if trades else None,
            fees=fees, slippage=slip, pnl_by_symbol=dict(by_sym), pnl_by_month=dict(sorted(by_month.items())))
    return Metrics(**m)


def _annual_vol(equity: list[EquityPoint], start_equity: Decimal) -> Decimal | None:
    """Standard deviation of daily returns (last mark of each UTC day) x sqrt(365)."""
    day_end: dict[int, Decimal] = {}
    for p in equity:
        day_end[(p.at_ms - 1) // DAY_MS] = p.equity
    values = [start_equity] + [day_end[k] for k in sorted(day_end)]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets, ZERO) / len(rets)
    var = sum(((r - mean) ** 2 for r in rets), ZERO) / (len(rets) - 1)
    return var.sqrt() * D(365).sqrt()


