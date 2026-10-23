"""The H2b forward test's end-of-test report (docs/h2b-plan.md section 3, rules fixed before any result).

Judged once, when the test has ended (12 months and 30 closed trades, or 24 months, or the drawdown
breaker tripped), on the forward account's own closed trades. All six must pass:
1. positive expectancy after doubled costs: net P&L per trade minus one more round of fees and
   slippage per trade is above zero;
2. the 6% drawdown breaker never trips;
3. at least 30 closed trades;
4. not driven by one coin: total profit positive, and still positive without the best coin;
5. not driven by one month: the same, without the best month;
6. beats random entries with the same exits on the same forward prices: above their 95th
   percentile per trade. The random runs replay the recorded forward months through the
   backtester; earlier candles are indicator history only (`engine.run(forward_from=...)`).
For information only: cash, buy-and-hold BTC, and a replay of H2b on the same months.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.analysis.indicators import percentile
from bot.backtest.engine import run
from bot.backtest.replay import CostModel
from bot.backtest.selection import Check
from bot.core.config import AppConfig
from bot.core.money import ZERO
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.forward import MIN_TRADES, ForwardTest, Progress, make_strategy, when
from bot.paper.sim import ClosedTrade, Fill
from bot.strategies.random_entry import RandomEntry

VERDICT_FILE = "verdict.json"
RANDOM_PERCENTILE = Decimal("0.95")


class ReportError(RuntimeError):
    pass


@dataclass
class ForwardEvaluation:
    trades: list[ClosedTrade]
    start_ms: int
    end_ms: int
    expectancy: Decimal | None
    doubled_expectancy: Decimal | None
    breaker_tripped_at: float | None
    by_coin: dict[str, Decimal]
    by_month: dict[str, Decimal]
    random_expectancies: list[Decimal]
    btc_buy_and_hold: Decimal | None
    replay_trades: int
    replay_expectancy: Decimal | None
    checks: list[Check] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return all(c.passed for c in self.checks)


def _expectancy(values: list[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def extra_costs(t: ClosedTrade, fills: list[Fill]) -> Decimal:
    """One more round of this trade's fees and slippage (the doubled-cost check)."""
    buy = [f for f in fills if f.side == "BUY" and f.symbol == t.symbol and f.at == t.opened_at]
    sell = [f for f in fills if f.side == "SELL" and f.symbol == t.symbol and f.at == t.closed_at]
    if len(buy) != 1 or len(sell) != 1:
        raise ReportError(f"cannot match the fills of the {t.symbol} trade closed at {t.closed_at}; "
                          f"the account's records are incomplete")
    return t.entry_fee + t.exit_fee + buy[0].slippage + sell[0].slippage


def evaluate(ft: ForwardTest, trades: list[ClosedTrade], fills: list[Fill], prog: Progress,
             candles: dict[str, list[Candle]], rules: dict[str, SymbolRules], cfg: AppConfig, end_ms: int,
             random_runs: int = 50, progress=None) -> ForwardEvaluation:
    say = progress or (lambda m: None)
    step = INTERVAL_MS["4h"]
    start_ms = -(-int(ft.started_at * 1000) // step) * step            # the first 4h candle after the start
    done = [t for t in trades if t.strategy == ft.strategy and t.closed_at * 1000 <= end_ms]
    by_coin, by_month = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for t in done:
        by_coin[t.symbol] += t.pnl
        by_month[datetime.fromtimestamp(t.closed_at, timezone.utc).strftime("%Y-%m")] += t.pnl
    strategy = make_strategy(ft)

    def replay(s):
        return run(s, candles, rules, cfg, CostModel(), start_ms=start_ms, end_ms=end_ms, edge=True,
                   corr_groups=dict(ft.corr_groups), forward_from=start_ms)

    say(f"H2b: {random_runs} random-entry runs with the same exits on the forward months")
    rand = []
    for seed in range(random_runs):
        e = _expectancy([x.pnl for x in replay(RandomEntry(list(ft.symbols), seed=seed, exits=strategy)).trades])
        if e is not None:
            rand.append(e)
    say("H2b: replay of the same months (information only)")
    h2b_replay = replay(strategy)
    btc = [c for c in candles.get("BTCUSDT", []) if c.open_time >= start_ms and c.close_time < end_ms]
    ev = ForwardEvaluation(
        trades=done, start_ms=start_ms, end_ms=end_ms, expectancy=_expectancy([t.pnl for t in done]),
        doubled_expectancy=_expectancy([t.pnl - extra_costs(t, fills) for t in done]),
        breaker_tripped_at=prog.failed_at, by_coin=dict(by_coin), by_month=dict(sorted(by_month.items())),
        random_expectancies=rand, btc_buy_and_hold=btc[-1].close / btc[0].open - 1 if btc else None,
        replay_trades=len(h2b_replay.trades), replay_expectancy=_expectancy([t.pnl for t in h2b_replay.trades]))
    ev.checks = checks(ev, cfg)
    return ev


def checks(ev: ForwardEvaluation, cfg: AppConfig) -> list[Check]:
    total = sum(ev.by_coin.values(), ZERO)
    out = [
        Check("Positive expectancy after doubled costs", ev.doubled_expectancy is not None and ev.doubled_expectancy > 0,
              f"{_usdt(ev.doubled_expectancy)} per trade over {len(ev.trades)} trades "
              f"(as traded {_usdt(ev.expectancy)})"),
        Check(f"The {cfg.risk.drawdown_breaker_pct}% drawdown breaker never trips", ev.breaker_tripped_at is None,
              "never tripped" if ev.breaker_tripped_at is None else f"tripped on {when(ev.breaker_tripped_at)}"),
        Check(f"At least {MIN_TRADES} closed trades", len(ev.trades) >= MIN_TRADES, f"{len(ev.trades)} trades"),
    ]
    for label, groups in (("coin", ev.by_coin), ("month", ev.by_month)):
        if not groups:
            out.append(Check(f"Not driven by one {label}", False, "no trades"))
            continue
        best = max(groups, key=lambda k: groups[k])
        rest = total - groups[best]
        out.append(Check(f"Not driven by one {label}", total > 0 and rest > 0,
                         f"total {_usdt(total)}; without {best} {_usdt(rest)}"))
    if ev.random_expectancies and ev.expectancy is not None:
        p95 = percentile(ev.random_expectancies, RANDOM_PERCENTILE)
        out.append(Check("Beats random entries with the same exits (95th percentile)", ev.expectancy > p95,
                         f"{_usdt(ev.expectancy)} vs random 95th percentile {_usdt(p95)} per trade "
                         f"({len(ev.random_expectancies)} runs)"))
    else:
        out.append(Check("Beats random entries with the same exits (95th percentile)", False, "not enough trades"))
    return out


def verdict(ev: ForwardEvaluation) -> str:
    if ev.passes:
        return ("PASSES the forward test. H2b becomes a candidate: next only if Sultan decides so, Stage 5 "
                "(testnet, 5 clean round trips) and Stage 6 (paper-trading review).")
    return "FAILS the forward test. H2b is rejected; no H2c is tested on these forward prices."


def _usdt(x: Decimal | None) -> str:
    return "–" if x is None else f"{x:+.2f} USDT"


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def render(ev: ForwardEvaluation, ft: ForwardTest, operator: str, generated: str) -> str:
    L = ["# H2b forward test: end-of-test report", "", f"**Verdict: {verdict(ev)}**", "",
         f"Forward test started {when(ft.started_at)} by {ft.operator}; judged {generated} by {operator}. "
         f"Trades closed {_day(ev.start_ms)} to {_day(ev.end_ms)} (simulated money, live prices). "
         f"Settings fixed at the start: {', '.join(f'{k}={v}' for k, v in ft.params.items())}. "
         f"Coins (correlation group): {'; '.join(f'{s} ({g})' for s, g in ft.corr_groups.items())}.", "",
         "## Checks (docs/h2b-plan.md section 3)", "", "| # | Check | Result | Detail |", "| --- | --- | --- | --- |"]
    L += [f"| {i} | {c.name} | {'pass' if c.passed else 'FAIL'} | {c.detail} |" for i, c in enumerate(ev.checks, 1)]
    L += ["", "## By coin", "", "| Coin | Net P&L |", "| --- | --- |"]
    L += [f"| {k} | {_usdt(v)} |" for k, v in sorted(ev.by_coin.items())] or ["| – | no trades |"]
    L += ["", "## By month", "", "| Month | Net P&L |", "| --- | --- |"]
    L += [f"| {k} | {_usdt(v)} |" for k, v in ev.by_month.items()] or ["| – | no trades |"]
    L += ["", "## For information (not checks)", "",
          "- Cash: +0.00%.",
          f"- Buy-and-hold BTC over the same months: "
          f"{'–' if ev.btc_buy_and_hold is None else f'{ev.btc_buy_and_hold * 100:+.1f}%'}.",
          f"- Replay of H2b on the same 4h candles: {ev.replay_trades} trades, {_usdt(ev.replay_expectancy)} per "
          f"trade. Live and replay can differ (live prices are checked every few seconds, the replay sees 4h "
          f"candles); the checks use the live trades.",
          "- Earlier candles (including the spent holdout year) were used only as indicator history for the "
          "random-entry replays; nothing before the forward start was traded or measured."]
    return "\n".join(L) + "\n"


def summary(ev: ForwardEvaluation) -> dict[str, Any]:
    return {"passes": ev.passes, "verdict": verdict(ev), "trades": len(ev.trades),
            "expectancy": None if ev.expectancy is None else str(ev.expectancy),
            "doubled_expectancy": None if ev.doubled_expectancy is None else str(ev.doubled_expectancy),
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in ev.checks],
            "by_coin": {k: str(v) for k, v in ev.by_coin.items()},
            "by_month": {k: str(v) for k, v in ev.by_month.items()},
            "random_expectancies": [str(x) for x in ev.random_expectancies],
            "period_ms": [ev.start_ms, ev.end_ms]}


def read_verdict(state_dir: Path) -> dict[str, Any] | None:
    path = Path(state_dir) / VERDICT_FILE
    return json.loads(path.read_text()) if path.exists() else None


def save_verdict(state_dir: Path, record: dict[str, Any]) -> None:
    """Written once: the forward test is judged once."""
    tmp = Path(state_dir) / f".{VERDICT_FILE}.tmp"
    tmp.write_text(json.dumps(record, indent=1))
    try:
        os.link(tmp, Path(state_dir) / VERDICT_FILE)
    except FileExistsError:
        raise ReportError("the forward test was already judged; its verdict is kept") from None
    finally:
        tmp.unlink(missing_ok=True)

