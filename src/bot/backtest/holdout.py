"""The one-time holdout test (module 4, step 3; rules approved by Sultan before any holdout result).

Only Sultan runs it, with a typed confirmation. Every opening is recorded in
`<data dir>/holdout-log.json` before any holdout candle is read. The holdout can be
used once in total: after the first opening, by any strategy, every later run is
marked VOID and cannot qualify (approved by Sultan, docs/h2b-plan.md section 5).

Procedure:
1. The strategy must have passed the walk-forward part of the selection rule
   with this config and these coins (its saved report is checked first).
2. Settings are chosen by the walk-forward method on the 24 months before the
   holdout (2023-10-01 to 2025-10-01), on research data only.
3. The holdout year (2025-10-01 to 2026-10-01) is run once with those settings:
   as deployed, edge measurement, and edge with doubled costs.
4. It passes only if:
   (a) expectancy per trade on the holdout is positive after doubled costs;
   (b) the 6% drawdown breaker never trips on the holdout, as deployed;
   (c) walk-forward and holdout trades together number at least 100;
   (d) together they are not driven by one coin, nor (e) by one month.
A failure rejects the strategy: nothing is adjusted and re-run.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.backtest.engine import BacktestResult, run
from bot.backtest.metrics import Metrics
from bot.backtest.periods import HOLDOUT_END_MS, HOLDOUT_START_MS, _TICKET_KEY, HoldoutLocked, HoldoutTicket
from bot.backtest.replay import CostModel
from bot.backtest.selection import MIN_TRADES, Check
from bot.backtest.walkforward import TRAIN_MONTHS, Window, _add, _ms, choose, metrics_of
from bot.core.config import AppConfig
from bot.core.money import ZERO, D, pct
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.strategies.base import Strategy

CONFIRM_PHRASE = "I am running the one-time holdout test"
LOG_FILE = "holdout-log.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def read_log(data_dir: Path) -> list[dict[str, Any]]:
    path = Path(data_dir) / LOG_FILE
    return json.loads(path.read_text()) if path.exists() else []


def _write_log(data_dir: Path, log: list[dict[str, Any]]) -> None:
    path = Path(data_dir) / LOG_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(log, indent=1))
    tmp.replace(path)


def open_holdout(data_dir: Path, strategy: str, operator: str, confirm: str) -> HoldoutTicket:
    """Record the opening, then hand out the ticket. Refused without the exact phrase and a name."""
    if confirm != CONFIRM_PHRASE:
        raise HoldoutLocked(f'type --confirm "{CONFIRM_PHRASE}" exactly to open the holdout')
    if not operator.strip():
        raise HoldoutLocked("the holdout needs an operator name")
    log = read_log(data_dir)
    previous = [f"{r['strategy']} at {r['started_at']}" for r in log]     # any strategy: the year is spent
    opened = _now()
    log.append({"strategy": strategy, "operator": operator, "started_at": opened, "status": "started",
                "void": bool(previous), "previous_runs": previous})
    _write_log(data_dir, log)
    return HoldoutTicket(operator, opened, previous, _TICKET_KEY)


def finish(data_dir: Path, strategy: str, ticket: HoldoutTicket, verdict: str, report: str) -> None:
    log = read_log(data_dir)
    for r in reversed(log):
        if r["strategy"] == strategy and r["started_at"] == ticket.opened_at:
            r.update(status="completed", finished_at=_now(), verdict=verdict, report=report)
            break
    _write_log(data_dir, log)


def find_walk_forward(reports_dir: Path, strategy: str, config_sha256: str, universe: str | None) -> tuple[Path, dict]:
    """The newest saved walk-forward report of this strategy, config and coins that passed."""
    for folder in sorted(Path(reports_dir).glob(f"*-strategy-{strategy}"), reverse=True):
        path = folder / "results.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        meta = data.get("meta", {})
        if (data.get("passes_walk_forward") and meta.get("config_sha256") == config_sha256
                and meta.get("universe") == universe):
            return folder, data
    raise HoldoutLocked(f"no passing walk-forward report for {strategy} with this config and these coins in "
                        f"{reports_dir}; the holdout is only for a strategy that passed walk-forward")


@dataclass
class HoldoutEvaluation:
    name: str
    params: dict[str, object]
    train_score: Decimal | None
    deployed: BacktestResult
    edge: BacktestResult
    edge_doubled: BacktestResult
    metrics: dict[str, Metrics]
    wf_folder: str
    wf_trades: int
    combined_trades: int
    combined_pnl: Decimal
    by_coin: dict[str, Decimal]
    by_month: dict[str, Decimal]
    void: bool
    previous_runs: list[str]
    checks: list[Check] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not self.void and all(c.passed for c in self.checks)


def evaluate(strategy: Strategy, research: dict[str, list[Candle]], through_holdout: dict[str, list[Candle]],
             rules: dict[str, SymbolRules], cfg: AppConfig, wf_folder: Path, wf: dict[str, Any],
             ticket: HoldoutTicket, cost: CostModel = CostModel(), corr_groups: dict[str, str] | None = None,
             progress=None) -> HoldoutEvaluation:
    say = progress or (lambda m: None)
    ty, tm = _add(2025, 10, -TRAIN_MONTHS)
    w = Window(_ms(ty, tm), HOLDOUT_START_MS, HOLDOUT_START_MS, HOLDOUT_END_MS)
    say(f"{strategy.name}: choosing settings on {ty}-{tm:02d} to 2025-09 (research data only)")
    chosen, sc = choose(strategy, research, rules, cfg, cost, w, corr_groups)
    say(f"{strategy.name}: settings {chosen.params()}; running the holdout year")
    common = dict(start_ms=HOLDOUT_START_MS, end_ms=HOLDOUT_END_MS, corr_groups=corr_groups, holdout=ticket)
    dep = run(chosen, through_holdout, rules, cfg, cost, edge=False, **common)
    edg = run(chosen, through_holdout, rules, cfg, cost, edge=True, **common)
    dbl = run(chosen, through_holdout, rules, cfg, cost.doubled(), edge=True, **common)
    by_coin = defaultdict(lambda: ZERO, {k: D(v) for k, v in wf["oos"]["by_coin"].items()})
    by_month = defaultdict(lambda: ZERO, {k: D(v) for k, v in wf["oos"]["by_month"].items()})
    for t in edg.trades:
        by_coin[t.symbol] += t.pnl
        by_month[datetime.fromtimestamp(t.closed_at, timezone.utc).strftime("%Y-%m")] += t.pnl
    ev = HoldoutEvaluation(
        name=strategy.name, params=chosen.params(), train_score=sc, deployed=dep, edge=edg, edge_doubled=dbl,
        metrics={"deployed": metrics_of(dep), "edge": metrics_of(edg), "edge_doubled": metrics_of(dbl)},
        wf_folder=str(wf_folder), wf_trades=int(wf["oos"]["trades"]),
        combined_trades=int(wf["oos"]["trades"]) + len(edg.trades),
        combined_pnl=D(wf["oos"]["pnl"]) + sum((t.pnl for t in edg.trades), ZERO),
        by_coin=dict(by_coin), by_month=dict(sorted(by_month.items())),
        void=ticket.void, previous_runs=list(ticket.previous_runs))
    ev.checks = checks(ev, cfg)
    return ev


def checks(ev: HoldoutEvaluation, cfg: AppConfig) -> list[Check]:
    limit = pct(cfg.risk.drawdown_breaker_pct)
    out = []
    e2 = ev.metrics["edge_doubled"].expectancy
    out.append(Check("Holdout: positive expectancy after doubled costs", e2 is not None and e2 > 0,
                     f"{_usdt(e2)} per trade over {ev.metrics['edge_doubled'].trades} trades"))
    dep = ev.metrics["deployed"]
    tripped = ev.deployed.drawdown_halt_ms is not None or dep.max_drawdown >= limit
    out.append(Check(f"Holdout: the {cfg.risk.drawdown_breaker_pct}% drawdown breaker never trips (as deployed)",
                     not tripped, f"max drawdown {dep.max_drawdown * 100:.1f}%" + (", breaker tripped" if tripped else "")))
    out.append(Check(f"Walk-forward + holdout: at least {MIN_TRADES} trades", ev.combined_trades >= MIN_TRADES,
                     f"{ev.wf_trades} + {ev.combined_trades - ev.wf_trades} = {ev.combined_trades} trades"))
    total = ev.combined_pnl
    for label, groups in (("coin", ev.by_coin), ("month", ev.by_month)):
        if not groups:
            out.append(Check(f"Walk-forward + holdout: not driven by one {label}", False, "no trades"))
            continue
        best = max(groups, key=lambda k: groups[k])
        rest = total - groups[best]
        out.append(Check(f"Walk-forward + holdout: not driven by one {label}", total > 0 and rest > 0,
                         f"total {_usdt(total)}; without {best} {_usdt(rest)}"))
    return out


def _usdt(x: Decimal | None) -> str:
    return "–" if x is None else f"{x:+.2f} USDT"


def _pc(x: Decimal | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def verdict(ev: HoldoutEvaluation) -> str:
    if ev.void:
        return ("VOID: the holdout was already used (" + ", ".join(ev.previous_runs) +
                "). This run cannot qualify anything.")
    if ev.passes:
        return ("PASSES the full selection rule (walk-forward and holdout). It may go to paper trading "
                "if Sultan decides so.")
    return f"FAILS the holdout. {ev.name} is rejected; it is not adjusted and re-run."


def render(ev: HoldoutEvaluation, meta: dict[str, Any], operator: str) -> str:
    L = [f"# {ev.name} holdout test", "", f"**Verdict: {verdict(ev)}**", "",
         f"Holdout 2025-10-01 to 2026-10-01, opened by {operator}. Settings chosen on the 24 months before "
         f"(research data only): {', '.join(f'{k}={v}' for k, v in ev.params.items())}. "
         f"Walk-forward report: {ev.wf_folder}. Generated {meta['generated_at']}, commit {meta['git_commit'][:10]}"
         f"{' (with uncommitted changes)' if meta['uncommitted_changes'] else ''}."
         + (f" Coins (correlation group): {meta['universe']}." if meta.get("universe") else ""), "",
         "## Checks", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    L += [f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {c.detail} |" for c in ev.checks]
    L += ["", "## Holdout year", "",
          "| Run | Trades | Net P&L | Expectancy | Net return | Max drawdown | Win rate | Fees |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label, key in (("As deployed", "deployed"), ("Edge measurement", "edge"), ("Doubled costs", "edge_doubled")):
        m = ev.metrics[key]
        L.append(f"| {label} | {m.trades} | {_usdt(m.end_equity - m.start_equity)} | {_usdt(m.expectancy)} | "
                 f"{_pc(m.net_return)} | {m.max_drawdown * 100:.1f}% | "
                 f"{'–' if m.win_rate is None else f'{m.win_rate * 100:.0f}%'} | {_usdt(m.fees)} |")
    L += ["", "## Walk-forward and holdout together, by coin", "", "| Coin | Net P&L |", "| --- | --- |"]
    L += [f"| {k} | {_usdt(v)} |" for k, v in sorted(ev.by_coin.items())]
    L += ["", "## Limits of simulation", ""] + [f"- {x}" for x in meta["limits_of_simulation"]]
    L += ["- The holdout is a single year: one more look at the strategy, not proof that it will keep working.",
          "", f"Config fingerprint (SHA-256): `{meta['config_sha256']}`"]
    return "\n".join(L) + "\n"


def summary(ev: HoldoutEvaluation, operator: str) -> dict[str, Any]:
    def m(x: Metrics) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in x.__dict__.items()
                if k not in ("pnl_by_symbol", "pnl_by_month")}
    return {"name": ev.name, "operator": operator, "verdict": verdict(ev), "passes": ev.passes, "void": ev.void,
            "previous_runs": ev.previous_runs, "params": {k: str(v) for k, v in ev.params.items()},
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in ev.checks],
            "holdout": {k: m(v) for k, v in ev.metrics.items()},
            "combined": {"trades": ev.combined_trades, "pnl": str(ev.combined_pnl),
                         "by_coin": {k: str(v) for k, v in ev.by_coin.items()},
                         "by_month": {k: str(v) for k, v in ev.by_month.items()}},
            "walk_forward_report": ev.wf_folder}
