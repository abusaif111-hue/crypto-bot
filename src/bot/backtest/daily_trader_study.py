"""H1-DailyTrader study: its three setups against D1, with the same selection rule as H2b and H4.

Each setup (BRK, CONT, RNG; `bot.strategies.daily_trader`) and the baseline D1 (`bot.strategies.daily_pullback`,
unchanged) goes through `selection.evaluate()`: chronological walk-forward (settings chosen on 24 months, tested
once on the next 6), robustness, per-coin runs and random entries with the same exits; the selection rule's
checks are unchanged. Every entry goes through `SimExchange.submit()` and so through `evaluate()`, with the
backtester's fees, spread and slippage, and again with doubled costs.

Periods (fixed before any run): candles up to 2024-10-01 only. The holdout (from 2025-10-01) is locked and
spent, and 2024-10 to 2025-10 is the final window reserved by the intraday plan (docs/intraday-plan.md), so
neither is read. Test windows: 2022-01 to 2024-10 (the last one is 3 months).

The setting grid, the windows and the checks are fixed here before any result is seen; a setup is not tuned
or re-run after its result. Results come only from a real run on the server.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.backtest import selection
from bot.backtest.replay import CostModel
from bot.backtest.walkforward import FIRST_TEST, TEST_MONTHS, TRAIN_MONTHS, Window, _add, _ms
from bot.core.config import AppConfig
from bot.core.money import ZERO
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.strategies.daily_pullback import DailyPullback
from bot.strategies.daily_trader import SETUPS, DailyTrader

STUDY_END_MS = _ms(2024, 10)                 # the intraday plan's reserved final window starts here
SYMBOLS = ("BTCUSDT", "ETHUSDT")
CORR_GROUPS = {"BTCUSDT": "majors", "ETHUSDT": "majors"}


def windows() -> list[Window]:
    """The walk-forward rule (24 months train, 6 months test, from 2022-01), ending at STUDY_END_MS."""
    out = []
    y, m = FIRST_TEST
    while _ms(y, m) < STUDY_END_MS:
        ty, tm = _add(y, m, -TRAIN_MONTHS)
        ey, em = _add(y, m, TEST_MONTHS)
        out.append(Window(_ms(ty, tm), _ms(y, m), _ms(y, m), min(_ms(ey, em), STUDY_END_MS)))
        y, m = ey, em
    return out


def study_only(candles: list[Candle]) -> list[Candle]:
    """Candles that closed before STUDY_END_MS: nothing from the reserved window or the holdout."""
    return [c for c in candles if c.close_time < STUDY_END_MS]


@dataclass
class Row:
    name: str
    interval: str
    ev: selection.StrategyEvaluation
    outcomes: Counter = field(default_factory=Counter)

    @property
    def profit_factor(self) -> Decimal | None:
        trades = [t for r in self.ev.windows for t in r.edge.trades]
        wins = sum((t.pnl for t in trades if t.pnl > 0), ZERO)
        losses = -sum((t.pnl for t in trades if t.pnl < 0), ZERO)
        return wins / losses if losses > 0 else None

    @property
    def worst_window_drawdown(self) -> Decimal:
        return max((r.metrics["deployed"].max_drawdown for r in self.ev.windows), default=ZERO)

    @property
    def costs(self) -> Decimal:
        return sum(((r.metrics["edge"].fees or ZERO) + (r.metrics["edge"].slippage or ZERO)
                    for r in self.ev.windows), ZERO)


def strategies() -> list[tuple[Any, str]]:
    """(strategy, interval of its candles): the three setups on 1h, D1 on 4h."""
    return [(DailyTrader(list(SYMBOLS), s), "1h") for s in SETUPS] + [(DailyPullback(list(SYMBOLS)), "4h")]


def run_study(candles: dict[str, dict[str, list[Candle]]], rules: dict[str, SymbolRules], cfg: AppConfig,
              cost: CostModel = CostModel(), random_runs: int = 50, progress=None, only=None) -> list[Row]:
    """`candles[interval][symbol]`; each is cut at STUDY_END_MS here, whatever the caller loaded."""
    ws = windows()
    rows = []
    for strategy, interval in strategies():
        if only is not None and strategy.name not in only:
            continue
        cs = {s: study_only(candles[interval][s]) for s in SYMBOLS}
        ev = selection.evaluate(strategy, cs, rules, cfg, cost, random_runs=random_runs, progress=progress,
                                ws=ws, corr_groups=CORR_GROUPS)
        outcomes = sum((r.edge.outcomes for r in ev.windows), Counter())
        rows.append(Row(strategy.name, interval, ev, outcomes))
    return rows


def _u(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:+.2f}"


def render(rows: list[Row]) -> str:
    w = windows()
    f = "%Y-%m"
    span = (f"{datetime.fromtimestamp(w[0].test_start / 1000, timezone.utc).strftime(f)} to "
            f"{datetime.fromtimestamp((w[-1].test_end - 1) / 1000, timezone.utc).strftime(f)}")
    L = [f"H1-DailyTrader study: out-of-sample walk-forward {span} ({len(w)} test windows), BTC and ETH.",
         "Candles before 2024-10-01 only (the holdout and the intraday plan's final window are not read).", "",
         f"{'Strategy':<10} {'Trades':>6} {'Exp/trade':>10} {'Doubled':>9} {'PF':>6} {'Worst DD':>9} "
         f"{'Costs':>9} {'Checks':>7}  Verdict"]
    for r in rows:
        ev = r.ev
        pf = "-" if r.profit_factor is None else f"{r.profit_factor:.2f}"
        passed = sum(c.passed for c in ev.checks)
        verdict = "passes walk-forward" if ev.passes_walk_forward else "does not qualify"
        L.append(f"{r.name:<10} {ev.oos_trades:>6} {_u(ev.oos_expectancy):>10} {_u(ev.oos_doubled_expectancy):>9} "
                 f"{pf:>6} {r.worst_window_drawdown * 100:>8.2f}% {r.costs:>9.2f} {passed:>3}/{len(ev.checks):<3}  "
                 f"{verdict}")
    L += ["", "Exp/trade: mean net P&L per out-of-sample trade (USDT, 10,000 USDT simulated account); Doubled: with",
          "every cost doubled; PF: profit factor; Worst DD: the worst test window's drawdown as deployed; Costs:",
          "fees and slippage over the test windows.", "", "Ideas and what the risk engine did with them "
          "(out-of-sample, edge runs):"]
    for r in rows:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(r.outcomes.items(), key=lambda kv: -kv[1])) or "no ideas"
        L.append(f"  {r.name}: {parts}")
    L += ["", "Checks per strategy (the selection rule, unchanged):"]
    for r in rows:
        L.append(f"  {r.name}:")
        L += [f"    [{'x' if c.passed else ' '}] {c.name}: {c.detail}" for c in r.ev.checks]
    L += ["", "D1 is the baseline, unchanged (its random entries use the backtester's default 3x target, not D1's 2x).",
          "Nothing here is a live or paper result. A setup that passes walk-forward goes next to a forward test",
          "on new prices, if Sultan approves one; none is tuned or re-run after this result."]
    return "\n".join(L) + "\n"


def summary(rows: list[Row]) -> dict[str, Any]:
    return {r.name: {"interval": r.interval, "passes_walk_forward": r.ev.passes_walk_forward,
                     "oos_trades": r.ev.oos_trades, "oos_expectancy": str(r.ev.oos_expectancy),
                     "oos_doubled_expectancy": str(r.ev.oos_doubled_expectancy),
                     "profit_factor": str(r.profit_factor), "worst_window_drawdown": str(r.worst_window_drawdown),
                     "costs": str(r.costs), "outcomes": dict(r.outcomes),
                     "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in r.ev.checks],
                     "selection": selection.summary(r.ev)} for r in rows}
