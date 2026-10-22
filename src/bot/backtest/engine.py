"""Backtest engine (module 4): replays history through the paper-trading simulator.

Same code path as paper trading (design package): the strategy proposes, and
`SimExchange.submit()` runs every idea through the risk engine and fills it
with the same fees, slippage and exits.

For each candle, in time order:
1. at its open: exits triggered by the opening price (a gap through a stop
   fills here), then the ideas proposed at the previous close are submitted and
   fill at the opening ask;
2. the candle itself: stop-loss / take-profit / time stop (stop first if one
   candle touches both);
3. at its close: equity is marked (breakers update) and the strategy, seeing
   only finished candles, proposes ideas for the next open.

Breakers: when a daily or weekly limit blocks entries, the run resumes entries
as soon as the breaker has cleared (modelling Sultan resuming after review).
The drawdown breaker needs a review and never clears in a backtest: trading
stops for the rest of the run, and the report says when.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from bot.backtest.periods import RESEARCH_START_MS, assert_no_holdout
from bot.backtest.replay import CostModel, ReplayMarket
from bot.core.config import AppConfig
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.paper.sim import STARTING_QUOTE, ClosedTrade, Fill, SimExchange
from bot.risk.models import Mode, RejectCode, TradeIntent
from bot.risk.modes import Transition
from bot.strategies.base import Strategy

POLICY_OPERATOR = "backtest-policy"


@dataclass(frozen=True)
class EquityPoint:
    at_ms: int
    equity: Decimal
    invested: bool


@dataclass
class BacktestResult:
    name: str
    params: dict[str, object]
    symbols: list[str]
    interval: str
    start_ms: int
    end_ms: int
    starting_equity: Decimal
    cost: CostModel
    equity: list[EquityPoint] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)      # filled / not filled / reject codes
    mode_events: list[Transition] = field(default_factory=list)
    drawdown_halt_ms: int | None = None
    open_at_end: list[str] = field(default_factory=list)


def run(strategy: Strategy, candles: dict[str, list[Candle]], rules: dict[str, SymbolRules], cfg: AppConfig,
        cost: CostModel = CostModel(), start_ms: int = RESEARCH_START_MS, end_ms: int | None = None,
        starting_quote: Decimal = STARTING_QUOTE) -> BacktestResult:
    for cs in candles.values():
        assert_no_holdout(cs)
    interval = strategy.interval
    market = ReplayMarket(candles, rules, cost, interval)
    by_time = {s: {c.open_time: c for c in cs} for s, cs in candles.items()}
    times = sorted({t for s in by_time for t in by_time[s]
                    if t >= start_ms and (end_ms is None or t + market.interval_ms <= end_ms)})
    if not times:
        raise ValueError("no candles in the requested period")
    symbols = sorted(candles)

    market.set_time(times[0])
    sim = SimExchange(cfg, market, clock=market.clock, starting_quote=starting_quote, fee_rate=cost.taker_fee,
                      entry_slippage_pct=cost.entry_slippage_pct, exit_slippage_pct=cost.exit_slippage_pct,
                      slippage_multiplier=cost.slippage_multiplier)
    sim.resume(POLICY_OPERATOR, "automatic start of a backtest run")
    result = BacktestResult(name=strategy.name, params=strategy.params(), symbols=symbols, interval=interval,
                            start_ms=times[0], end_ms=times[-1] + market.interval_ms,
                            starting_equity=sim.quote, cost=cost)
    pending: list[TradeIntent] = []

    for t in times:
        market.set_time(t)
        now = t / 1000
        held = [s for s in sim.positions if t in by_time[s]]
        if held:
            for s, book in market.books(held).items():          # 1. gaps at the open
                sim.on_book(s, book, now)
        for intent in pending:                                    # 1. last close's ideas fill at this open
            r = sim.submit(intent, now)
            result.outcomes["filled" if r.filled else
                            ("not filled" if r.decision.accepted else r.decision.code.value)] += 1
        pending = []
        for s in symbols:                                         # 2. the candle itself
            c = by_time[s].get(t)
            if c is not None:
                sim.on_candle(s, c)
        close_ms = t + market.interval_ms                         # 3. at the close
        market.set_time(close_ms)
        _apply_resume_policy(sim, close_ms / 1000)
        if result.drawdown_halt_ms is None and RejectCode.DRAWDOWN.value in sim.modes.active_breaches:
            result.drawdown_halt_ms = close_ms
        result.equity.append(EquityPoint(close_ms, sim.equity(), bool(sim.positions)))
        pending = strategy.propose(sim.data, close_ms / 1000)

    result.trades = list(sim.trades)
    result.fills = list(sim.fills)
    result.mode_events = list(sim.modes.history)
    result.open_at_end = sorted(sim.positions)
    return result


def _apply_resume_policy(sim: SimExchange, now: float) -> None:
    sim.account_state(now)                       # day/week rollovers clear daily/weekly breakers
    if sim.modes.mode is Mode.ENTRIES_BLOCKED and not sim.modes.active_breaches:
        try:
            sim.resume(POLICY_OPERATOR, "resumed automatically once no breaker was active", now)
        except PermissionError:
            pass
