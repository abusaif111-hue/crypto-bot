"""Strategy evaluation against the selection rule (design package + Sultan's decisions).

A strategy may go to paper trading only if, on walk-forward AND the holdout:
1. net expectancy per trade is positive after doubled costs;
2. maximum drawdown stays within the risk budget: every test window, as deployed,
   stays below the 6% drawdown breaker (Sultan's decision);
3. at least 100 simulated (out-of-sample) trades exist;
4. the result is not driven by one coin: without the best coin's P&L, the rest is positive;
5. ... nor by one month: without the best month's P&L, the rest is positive.
Also required (design package, "Robustness" and "Baselines"):
6. no setting moved by 20% makes the result collapse (expectancy turns negative or
   falls below half);
7. the strategy beats random entries with the same exits (above their 95th percentile).
The holdout part is step 3 and is run only by Sultan; until then the verdict is
at most "passes walk-forward, holdout pending". The best return is never a criterion.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.analysis.indicators import percentile, realized_vol, sma
from bot.analysis.resample import DAY_MS, daily
from bot.backtest.engine import run
from bot.backtest.metrics import Metrics
from bot.backtest.periods import RESEARCH_START_MS
from bot.backtest.replay import CostModel
from bot.backtest.walkforward import Window, WindowResult, metrics_of, one_at_a_time, walk_forward
from bot.core.config import AppConfig
from bot.core.money import ZERO, D, pct
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.paper.sim import ClosedTrade
from bot.strategies.base import Strategy
from bot.strategies.random_entry import RandomEntry

MIN_TRADES = 100
COLLAPSE_SHARE = Decimal("0.5")
RANDOM_PERCENTILE = Decimal("0.95")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class StrategyEvaluation:
    name: str
    default_params: dict[str, object]
    windows: list[WindowResult]
    oos_trades: int
    oos_pnl: Decimal
    oos_expectancy: Decimal | None
    oos_doubled_trades: int
    oos_doubled_expectancy: Decimal | None
    full_edge: Metrics
    full_deployed: Metrics
    full_deployed_halt_ms: int | None
    full_drawdown_events: int
    sensitivity: list[tuple[str, Metrics]]
    per_coin: dict[str, Metrics]
    regimes: dict[str, tuple[int, Decimal]]
    random_expectancies: list[Decimal]
    by_coin: dict[str, Decimal]
    by_month: dict[str, Decimal]
    checks: list[Check] = field(default_factory=list)

    @property
    def passes_walk_forward(self) -> bool:
        return all(c.passed for c in self.checks)


def _expectancy(trades: list[ClosedTrade]) -> Decimal | None:
    return sum((t.pnl for t in trades), ZERO) / len(trades) if trades else None


def evaluate(strategy: Strategy, candles: dict[str, list[Candle]], rules: dict[str, SymbolRules], cfg: AppConfig,
             cost: CostModel = CostModel(), random_runs: int = 50, progress=None,
             ws: list[Window] | None = None, corr_groups: dict[str, str] | None = None) -> StrategyEvaluation:
    say = progress or (lambda msg: None)
    wf = walk_forward(strategy, candles, rules, cfg, cost, progress=say, ws=ws, corr_groups=corr_groups)
    oos = [t for r in wf for t in r.edge.trades]
    oos_dbl = [t for r in wf for t in r.edge_doubled.trades]
    by_coin, by_month = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for t in oos:
        by_coin[t.symbol] += t.pnl
        by_month[datetime.fromtimestamp(t.closed_at, timezone.utc).strftime("%Y-%m")] += t.pnl

    say(f"{strategy.name}: full research period, robustness and per-coin runs")
    full_edge_res = run(strategy, candles, rules, cfg, cost, RESEARCH_START_MS, edge=True, corr_groups=corr_groups)
    full_dep_res = run(strategy, candles, rules, cfg, cost, RESEARCH_START_MS, edge=False, corr_groups=corr_groups)
    sensitivity = [(label, metrics_of(run(v, candles, rules, cfg, cost, RESEARCH_START_MS, edge=True, corr_groups=corr_groups)))
                   for label, v in one_at_a_time(strategy)]
    per_coin = {}
    for s in candles:
        solo = _with_symbols(strategy, [s])
        per_coin[s] = metrics_of(run(solo, {s: candles[s]}, {s: rules[s]}, cfg, cost, RESEARCH_START_MS, edge=True, corr_groups=corr_groups))

    say(f"{strategy.name}: {random_runs} random-entry runs with the same exits")
    rand = []
    for seed in range(random_runs):
        r = run(RandomEntry(sorted(candles), seed=seed, exits=strategy), candles, rules, cfg, cost,
                RESEARCH_START_MS, edge=True, corr_groups=corr_groups)
        e = _expectancy(r.trades)
        if e is not None:
            rand.append(e)

    ev = StrategyEvaluation(
        name=strategy.name, default_params=strategy.params(), windows=wf,
        oos_trades=len(oos), oos_pnl=sum((t.pnl for t in oos), ZERO), oos_expectancy=_expectancy(oos),
        oos_doubled_trades=len(oos_dbl), oos_doubled_expectancy=_expectancy(oos_dbl),
        full_edge=metrics_of(full_edge_res), full_deployed=metrics_of(full_dep_res),
        full_deployed_halt_ms=full_dep_res.drawdown_halt_ms, full_drawdown_events=len(full_edge_res.drawdown_events),
        sensitivity=sensitivity, per_coin=per_coin, regimes=regime_breakdown(full_edge_res.trades, candles),
        random_expectancies=rand, by_coin=dict(by_coin), by_month=dict(sorted(by_month.items())))
    ev.checks = checks(ev, cfg)
    return ev


def _with_symbols(strategy: Strategy, symbols: list[str]) -> Strategy:
    return replace(strategy, symbols=symbols)


def checks(ev: StrategyEvaluation, cfg: AppConfig) -> list[Check]:
    limit = pct(cfg.risk.drawdown_breaker_pct)
    out = []
    e2 = ev.oos_doubled_expectancy
    out.append(Check("Positive expectancy after doubled costs (walk-forward)", e2 is not None and e2 > 0,
                     f"{_usdt(e2)} per trade over {ev.oos_doubled_trades} trades"))
    bad = [r.window.label for r in ev.windows
           if r.deployed.drawdown_halt_ms is not None or r.metrics["deployed"].max_drawdown >= limit]
    out.append(Check(f"Every test window stays below the {cfg.risk.drawdown_breaker_pct}% drawdown breaker",
                     not bad, "all windows below" if not bad else "breached in " + ", ".join(bad)))
    out.append(Check(f"At least {MIN_TRADES} out-of-sample trades", ev.oos_trades >= MIN_TRADES,
                     f"{ev.oos_trades} trades"))
    total = ev.oos_pnl
    if ev.by_coin:
        best_coin = max(ev.by_coin, key=lambda k: ev.by_coin[k])
        rest = total - ev.by_coin[best_coin]
        out.append(Check("Not driven by one coin", total > 0 and rest > 0,
                         f"total {_usdt(total)}; without {best_coin} {_usdt(rest)}"))
    else:
        out.append(Check("Not driven by one coin", False, "no trades"))
    if ev.by_month:
        best_month = max(ev.by_month, key=lambda k: ev.by_month[k])
        rest = total - ev.by_month[best_month]
        out.append(Check("Not driven by one month", total > 0 and rest > 0,
                         f"without {best_month} {_usdt(rest)}"))
    else:
        out.append(Check("Not driven by one month", False, "no trades"))
    base = ev.full_edge.expectancy
    collapsed = [label for label, m in ev.sensitivity
                 if base is None or m.expectancy is None or m.expectancy <= 0 or m.expectancy < base * COLLAPSE_SHARE]
    out.append(Check("Robust: no setting moved 20% collapses the result", base is not None and base > 0 and not collapsed,
                     "no collapse" if not collapsed and base and base > 0 else
                     ("base expectancy not positive" if not base or base <= 0 else "collapses at " + ", ".join(collapsed))))
    if ev.random_expectancies and base is not None:
        p95 = percentile(ev.random_expectancies, RANDOM_PERCENTILE)
        out.append(Check("Beats random entries with the same exits (95th percentile)", base > p95,
                         f"{_usdt(base)} vs random 95th percentile {_usdt(p95)} per trade"))
    else:
        out.append(Check("Beats random entries with the same exits (95th percentile)", False, "not enough trades"))
    return out


# ---------------------------------------------------------------- regimes (labelling rules fixed before testing)
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


def regime_breakdown(trades: list[ClosedTrade], candles: dict[str, list[Candle]]) -> dict[str, tuple[int, Decimal]]:
    labels = {s: regime_labels(cs) for s, cs in candles.items()}
    agg: dict[str, list[Decimal]] = defaultdict(list)
    for t in trades:
        day = int(t.opened_at * 1000) // DAY_MS * DAY_MS - DAY_MS       # the last complete day before entry
        label, high_vol = labels[t.symbol].get(day, ("unlabelled", False))
        agg[label].append(t.pnl)
        if high_vol:
            agg["high volatility"].append(t.pnl)
    return {k: (len(v), sum(v, ZERO) / len(v)) for k, v in sorted(agg.items())}


# ---------------------------------------------------------------- rendering
def _usdt(x: Decimal | None) -> str:
    return "–" if x is None else f"{x:+.2f} USDT"


def _pc(x: Decimal | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def render(ev: StrategyEvaluation, meta: dict[str, Any], cfg: AppConfig) -> str:
    verdict = ("PASSES the walk-forward part of the selection rule. Next: the holdout test, run only by Sultan."
               if ev.passes_walk_forward else
               "DOES NOT QUALIFY for paper trading on walk-forward. It stays out of the bot.")
    L = [f"# {ev.name} backtest", "",
         f"**Verdict: {verdict}**", "",
         f"Research period {meta['research_period'][0]} to {meta['research_period'][1]}; holdout {meta['holdout']}. "
         f"Generated {meta['generated_at']}, commit {meta['git_commit'][:10]}"
         f"{' (with uncommitted changes)' if meta['uncommitted_changes'] else ''}. "
         f"Default settings: {', '.join(f'{k}={v}' for k, v in ev.default_params.items())}."
         + (f" Coins (correlation group): {meta['universe']}." if meta.get("universe") else ""), "",
         "## Selection rule", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    L += [f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {c.detail} |" for c in ev.checks]
    L += ["| Holdout (2025-10 to 2026-10) | pending | step 3, run only by Sultan |", "",
          "## Walk-forward test windows (unseen data, settings chosen on the 24 months before)", "",
          "| Test window | Settings chosen | Trades | Net P&L | Expectancy | Max drawdown (as deployed) | Breaker | "
          "Expectancy, doubled costs |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in ev.windows:
        e, d, x = r.metrics["edge"], r.metrics["deployed"], r.metrics["edge_doubled"]
        halted = "tripped" if r.deployed.drawdown_halt_ms is not None else "–"
        L.append(f"| {r.window.label} | {', '.join(f'{k}={v}' for k, v in r.params.items())} | {e.trades} | "
                 f"{_usdt(e.end_equity - e.start_equity)} | {_usdt(e.expectancy)} | {d.max_drawdown * 100:.1f}% | "
                 f"{halted} | {_usdt(x.expectancy)} |")
    L += ["", f"Out-of-sample total: {ev.oos_trades} trades, {_usdt(ev.oos_pnl)}, expectancy {_usdt(ev.oos_expectancy)} "
          f"per trade; with doubled costs {_usdt(ev.oos_doubled_expectancy)}.", "",
          "## Whole research period, default settings", "",
          "| Run | Net return | CAGR | Max drawdown | Trades | Win rate | Profit factor | Expectancy | Fees | Time invested |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label, m in (("Edge measurement", ev.full_edge), ("As deployed", ev.full_deployed)):
        L.append(f"| {label} | {_pc(m.net_return)} | {_pc(m.cagr)} | {m.max_drawdown * 100:.1f}% | {m.trades} | "
                 f"{'–' if m.win_rate is None else f'{m.win_rate * 100:.0f}%'} | "
                 f"{'–' if m.profit_factor is None else f'{m.profit_factor:.2f}'} | {_usdt(m.expectancy)} | "
                 f"{_usdt(m.fees)} | {m.exposure * 100:.0f}% |")
    halt = ev.full_deployed_halt_ms
    L += ["", ("As deployed, the drawdown breaker stopped trading on "
               f"{datetime.fromtimestamp(halt / 1000, timezone.utc):%Y-%m-%d}." if halt else
               "As deployed, the drawdown breaker never tripped.") +
          f" In edge measurement it was breached {ev.full_drawdown_events} time(s).", "",
          "## Robustness: each setting moved 20%", "", "| Change | Trades | Expectancy | Net return |",
          "| --- | --- | --- | --- |", f"| defaults | {ev.full_edge.trades} | {_usdt(ev.full_edge.expectancy)} | "
          f"{_pc(ev.full_edge.net_return)} |"]
    L += [f"| {label} | {m.trades} | {_usdt(m.expectancy)} | {_pc(m.net_return)} |" for label, m in ev.sensitivity]
    L += ["", "## Per coin (traded alone)", "", "| Coin | Trades | Expectancy | Net return | Max drawdown |",
          "| --- | --- | --- | --- | --- |"]
    L += [f"| {s} | {m.trades} | {_usdt(m.expectancy)} | {_pc(m.net_return)} | {m.max_drawdown * 100:.1f}% |"
          for s, m in ev.per_coin.items()]
    L += ["", "## By market regime at entry (labels fixed before testing)", "", "| Regime | Trades | Expectancy |",
          "| --- | --- | --- |"]
    L += [f"| {k} | {n} | {_usdt(e)} |" for k, (n, e) in ev.regimes.items()]
    if ev.random_expectancies:
        q = [percentile(ev.random_expectancies, D(x)) for x in ("0.05", "0.5", "0.95")]
        L += ["", f"## Random entries with the same exits ({len(ev.random_expectancies)} runs)", "",
              f"Expectancy per trade: 5th percentile {_usdt(q[0])}, median {_usdt(q[1])}, 95th percentile {_usdt(q[2])}. "
              f"{ev.name} with default settings: {_usdt(ev.full_edge.expectancy)}."]
    L += ["", "## Cost model", ""] + [f"- {k}: {v}" for k, v in meta["cost_model"].items()]
    L += ["", "## Limits of simulation", ""] + [f"- {x}" for x in meta["limits_of_simulation"]]
    L += ["- Edge measurement resets the drawdown peak after each breach to keep measuring; paper and live "
          "trading have no such reset.",
          "- Settings are chosen on training data only; the selection rule never looks at the holdout.",
          "", f"Config fingerprint (SHA-256): `{meta['config_sha256']}`"]
    return "\n".join(L) + "\n"


def summary(ev: StrategyEvaluation) -> dict[str, Any]:
    def m(x: Metrics) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in x.__dict__.items()
                if k not in ("pnl_by_symbol", "pnl_by_month")}
    return {
        "name": ev.name, "default_params": {k: str(v) for k, v in ev.default_params.items()},
        "passes_walk_forward": ev.passes_walk_forward,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in ev.checks],
        "windows": [{"test": r.window.label, "params": {k: str(v) for k, v in r.params.items()},
                     "train_score": None if r.train_score is None else str(r.train_score),
                     "deployed_halted": r.deployed.drawdown_halt_ms is not None,
                     **{k: m(v) for k, v in r.metrics.items()}} for r in ev.windows],
        "oos": {"trades": ev.oos_trades, "pnl": str(ev.oos_pnl),
                "expectancy": None if ev.oos_expectancy is None else str(ev.oos_expectancy),
                "doubled_expectancy": None if ev.oos_doubled_expectancy is None else str(ev.oos_doubled_expectancy),
                "by_coin": {k: str(v) for k, v in ev.by_coin.items()},
                "by_month": {k: str(v) for k, v in ev.by_month.items()}},
        "full_edge": m(ev.full_edge), "full_deployed": m(ev.full_deployed),
        "sensitivity": {label: m(x) for label, x in ev.sensitivity},
        "per_coin": {k: m(v) for k, v in ev.per_coin.items()},
        "regimes": {k: {"trades": n, "expectancy": str(e)} for k, (n, e) in ev.regimes.items()},
        "random_expectancies": [str(x) for x in ev.random_expectancies],
    }
