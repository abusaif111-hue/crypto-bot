"""Backtest reports (module 4).

Every report records the git commit, a fingerprint of the config, the data
range, the cost model and the limits of simulation (design package). Reports are
written to a folder on the server as `report.md` (for people) and
`results.json` (for later comparison); nothing in them is typed by hand.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.backtest import benchmarks
from bot.backtest.engine import BacktestResult, run
from bot.backtest.metrics import Metrics, compute, percentile
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS
from bot.backtest.replay import CostModel
from bot.core.config import AppConfig
from bot.core.money import D, fmt
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.paper.sim import STARTING_QUOTE
from bot.strategies.random_entry import RandomEntry

LIMITS = (
    "Simulated, not real trading: fills assume the order executes at the modelled price.",
    "Order-book depth is not available for the past; it is assumed to be a fixed share of 24h volume.",
    "The spread is a fixed half-spread; real spreads widen in fast markets.",
    "Exchange rules (tick, step, minimum order) are today's, applied to the whole history.",
    "When one candle touches both the stop and the target, the stop is assumed to fill first.",
    "Daily and weekly breakers are resumed automatically once cleared; the drawdown breaker stops the run.",
    "Major-news periods are not broken out yet (no news data until the news module).",
    "Past results do not predict future results.",
)


def provenance(cfg: AppConfig, candles: dict[str, list[Candle]], cost: CostModel) -> dict[str, Any]:
    try:
        repo = Path(__file__).resolve().parents[3]
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                                timeout=10).stdout.strip() or "unknown"
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        commit, dirty = "unknown", True
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "git_commit": commit, "uncommitted_changes": dirty,
        "config_sha256": hashlib.sha256(cfg.model_dump_json().encode()).hexdigest(),
        "data": {s: {"first": _day(cs[0].open_time), "last": _day(cs[-1].close_time), "candles": len(cs)}
                 for s, cs in candles.items()},
        "research_period": [_day(RESEARCH_START_MS), _day(HOLDOUT_START_MS)],
        "holdout": "locked (not used)",
        "cost_model": {k: (str(v) if v is not None else "risk config default") for k, v in asdict(cost).items()},
        "limits_of_simulation": list(LIMITS),
    }


def run_baselines(candles: dict[str, list[Candle]], rules: dict[str, SymbolRules], cfg: AppConfig,
                  seeds: int = 200, cost: CostModel = CostModel(), start_ms: int = RESEARCH_START_MS,
                  end_ms: int | None = None, progress=None) -> dict[str, Any]:
    start = STARTING_QUOTE
    rows: dict[str, Metrics] = {
        "Cash (USDT)": compute(benchmarks.cash(candles, start_ms, end_ms, start), start, start_ms),
    }
    if "BTCUSDT" in candles:
        rows["Buy and hold BTC"] = compute(
            benchmarks.buy_and_hold(candles, "BTCUSDT", cfg, cost, start_ms, end_ms, start), start, start_ms)
    if {"BTCUSDT", "ETHUSDT"} <= set(candles):
        rows["50/50 BTC/ETH, monthly rebalance"] = compute(
            benchmarks.fifty_fifty_monthly(candles, cfg, cost, start_ms, end_ms, start), start, start_ms)
    randoms: list[Metrics] = []
    halts = 0
    for seed in range(seeds):
        res = run(RandomEntry(sorted(candles), seed=seed), candles, rules, cfg, cost, start_ms, end_ms)
        randoms.append(result_metrics(res))
        halts += res.drawdown_halt_ms is not None
        if progress:
            progress(seed + 1, seeds)
    return {"benchmarks": rows, "random": randoms, "random_drawdown_halts": halts}


def result_metrics(res: BacktestResult) -> Metrics:
    return compute(res.equity, res.starting_equity, res.start_ms, res.trades, res.fills)


def _pc(x: Decimal | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def _row(name: str, m: Metrics) -> str:
    trades = "–" if m.trades is None else str(m.trades)
    return (f"| {name} | {_pc(m.net_return)} | {_pc(m.cagr)} | {m.max_drawdown * 100:.1f}% | "
            f"{m.max_drawdown_days:.0f} | {'–' if m.annual_vol is None else f'{m.annual_vol * 100:.1f}%'} | "
            f"{m.exposure * 100:.0f}% | {trades} |")


def render(results: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = ["# Baselines backtest", "",
             f"Research period {meta['research_period'][0]} to {meta['research_period'][1]}; "
             f"holdout {meta['holdout']}. Start 10,000 USDT. Generated {meta['generated_at']}, "
             f"commit {meta['git_commit'][:10]}{' (with uncommitted changes)' if meta['uncommitted_changes'] else ''}.",
             "",
             "| Baseline | Net return | CAGR | Max drawdown | Longest below peak (days) | Volatility (yearly) "
             "| Time invested | Trades |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, m in results["benchmarks"].items():
        lines.append(_row(name, m))
    rs = results["random"]
    if rs:
        lines += ["", f"## Random entries, same exits and sizing ({len(rs)} runs, seeds 0-{len(rs) - 1})", "",
                  "| Measure | 5th percentile | Median | 95th percentile |", "| --- | --- | --- | --- |"]
        for label, get, as_pc in (("Net return", lambda m: m.net_return, True),
                                  ("Max drawdown", lambda m: m.max_drawdown, True),
                                  ("Trades", lambda m: D(m.trades), False),
                                  ("Expectancy per trade (USDT)", lambda m: m.expectancy or D(0), False),
                                  ("Fees paid (USDT)", lambda m: m.fees, False)):
            vals = [get(m) for m in rs]
            cells = [percentile(vals, D(q)) for q in ("0.05", "0.5", "0.95")]
            lines.append(f"| {label} | " + " | ".join(_pc(c) if as_pc else fmt(c) for c in cells) + " |")
        lines.append(f"\nRuns stopped by the drawdown breaker: {results['random_drawdown_halts']} of {len(rs)}.")
        lines.append("A strategy that cannot beat these random entries, with the same exits and risk rules, "
                     "has no edge in its entries.")
    lines += ["", "## Data", ""]
    for s, d in meta["data"].items():
        lines.append(f"- {s}: {d['candles']} candles, {d['first']} to {d['last']}")
    lines += ["", "## Cost model", ""]
    lines += [f"- {k}: {v}" for k, v in meta["cost_model"].items()]
    lines += ["", "## Limits of simulation", ""] + [f"- {x}" for x in meta["limits_of_simulation"]]
    lines += ["", f"Config fingerprint (SHA-256): `{meta['config_sha256']}`"]
    return "\n".join(lines) + "\n"


def write(out_dir: Path, name: str, results: dict[str, Any], meta: dict[str, Any]) -> Path:
    folder = Path(out_dir) / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{name}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text(render(results, meta))
    data = {"meta": meta,
            "benchmarks": {k: _plain(asdict(m)) for k, m in results["benchmarks"].items()},
            "random": [_plain(asdict(m)) for m in results["random"]],
            "random_drawdown_halts": results["random_drawdown_halts"]}
    (folder / "results.json").write_text(json.dumps(data, indent=1))
    return folder


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
