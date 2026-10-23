"""Walk-forward testing (design package, "Method: Splits").

Rolling windows inside the research period: settings are chosen on 24 months
of training data, then tested once on the next 6 unseen months; the windows
step forward 6 months at a time. Test windows: 2022-01 to 2025-10 (the last one
is 3 months, ending where the holdout starts).

Choosing settings: each tunable setting at 80%, 100% and 120% of its default
(27 combinations), run in edge mode on the training window. The pick maximises
net profit / maximum drawdown with at least 20 training trades; with none
qualifying, the defaults are kept. The best return alone is never the criterion.

Each test window is run three ways with the chosen settings: as deployed (the
6% drawdown breaker halts trading), edge measurement, and edge with doubled costs.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from bot.backtest.engine import BacktestResult, run
from bot.backtest.metrics import Metrics, compute
from bot.backtest.periods import HOLDOUT_START_MS
from bot.backtest.replay import CostModel
from bot.core.config import AppConfig
from bot.core.money import ZERO, D
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.strategies.base import Strategy, scaled

TRAIN_MONTHS = 24
TEST_MONTHS = 6
FIRST_TEST = (2022, 1)
GRID = (Decimal("0.8"), Decimal("1"), Decimal("1.2"))
MIN_TRAIN_TRADES = 20


def _ms(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _add(y: int, m: int, months: int) -> tuple[int, int]:
    k = y * 12 + (m - 1) + months
    return k // 12, k % 12 + 1


@dataclass(frozen=True)
class Window:
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def label(self) -> str:
        f = "%Y-%m"
        a = datetime.fromtimestamp(self.test_start / 1000, timezone.utc).strftime(f)
        b = datetime.fromtimestamp((self.test_end - 1) / 1000, timezone.utc).strftime(f)
        return f"{a} to {b}"


def windows() -> list[Window]:
    out = []
    y, m = FIRST_TEST
    while _ms(y, m) < HOLDOUT_START_MS:
        ty, tm = _add(y, m, -TRAIN_MONTHS)
        ey, em = _add(y, m, TEST_MONTHS)
        out.append(Window(_ms(ty, tm), _ms(y, m), _ms(y, m), min(_ms(ey, em), HOLDOUT_START_MS)))
        y, m = ey, em
    return out


def variants(strategy: Strategy) -> list[Strategy]:
    """Every combination of each tunable setting at 80/100/120%."""
    names = list(strategy.params())
    base = strategy.params()
    return [strategy.with_params(**{n: scaled(base[n], f) for n, f in zip(names, combo)})
            for combo in itertools.product(GRID, repeat=len(names))]


def one_at_a_time(strategy: Strategy) -> list[tuple[str, Strategy]]:
    """Each setting moved by -20% and +20% on its own (robustness check)."""
    out = []
    for n, v in strategy.params().items():
        for f in (Decimal("0.8"), Decimal("1.2")):
            out.append((f"{n} x{f}", strategy.with_params(**{n: scaled(v, f)})))
    return out


def score(m: Metrics) -> Decimal | None:
    if (m.trades or 0) < MIN_TRAIN_TRADES:
        return None
    return (m.end_equity - m.start_equity) / max(m.max_drawdown, D("0.01"))


@dataclass
class WindowResult:
    window: Window
    params: dict[str, object]
    train_score: Decimal | None
    deployed: BacktestResult
    edge: BacktestResult
    edge_doubled: BacktestResult
    metrics: dict[str, Metrics] = field(default_factory=dict)


def metrics_of(res: BacktestResult) -> Metrics:
    return compute(res.equity, res.starting_equity, res.start_ms, res.trades, res.fills)


def choose(strategy: Strategy, candles, rules, cfg, cost, w: Window,
           corr_groups: dict[str, str] | None = None) -> tuple[Strategy, Decimal | None]:
    best, best_score = strategy, None
    for v in variants(strategy):
        s = score(metrics_of(run(v, candles, rules, cfg, cost, w.train_start, w.train_end, edge=True, corr_groups=corr_groups)))
        if s is not None and (best_score is None or s > best_score):
            best, best_score = v, s
    return best, best_score


def walk_forward(strategy: Strategy, candles: dict[str, list[Candle]], rules: dict[str, SymbolRules],
                 cfg: AppConfig, cost: CostModel = CostModel(), progress=None,
                 ws: list[Window] | None = None, corr_groups: dict[str, str] | None = None) -> list[WindowResult]:
    out = []
    ws = windows() if ws is None else ws
    for i, w in enumerate(ws):
        chosen, sc = choose(strategy, candles, rules, cfg, cost, w, corr_groups)
        dep = run(chosen, candles, rules, cfg, cost, w.test_start, w.test_end, edge=False, corr_groups=corr_groups)
        edg = run(chosen, candles, rules, cfg, cost, w.test_start, w.test_end, edge=True, corr_groups=corr_groups)
        dbl = run(chosen, candles, rules, cfg, cost.doubled(), w.test_start, w.test_end, edge=True, corr_groups=corr_groups)
        out.append(WindowResult(w, chosen.params(), sc, dep, edg, dbl,
                                {"deployed": metrics_of(dep), "edge": metrics_of(edg), "edge_doubled": metrics_of(dbl)}))
        if progress:
            progress(f"{strategy.name}: walk-forward window {i + 1} of {len(ws)} ({w.label}) done")
    return out


def pooled_pnl(results: list[WindowResult], key: str) -> tuple[int, Decimal]:
    trades = [t for r in results for t in getattr(r, key).trades]
    return len(trades), sum((t.pnl for t in trades), ZERO)
