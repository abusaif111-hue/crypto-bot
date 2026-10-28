"""DailyTradePlanner: complete daily trade plans for the 20 USDT live pilot (Sultan's request, 28 Oct 2026).

For each coin (BTC and ETH) it turns D1's verdict (`bot.strategies.daily_pullback`, unchanged) into a full plan:
entry, stop-loss, take-profit, size, net reward-to-risk, maximum planned loss, estimated fees and slippage, the
order's expiry and what invalidates the setup. Every number that decides a trade comes from the risk engine
(`evaluate()`, unchanged, with the configured limits); a plan the engine rejects is NO TRADE with its reason.

It also answers whether the pilot can trade at all: the smallest order Binance accepts (minimum notional and
step size, from exchangeInfo), what it would lose at the stop, and the equity each limit needs for it. If that
order needs more risk than allowed, the plan says so; the risk is never raised to fit it.

Read-only: it places no order and touches no account; it is not wired into any runner. Each run is appended
to its own log (`plans.jsonl`), never to a runner's folder. The pilot allocation is at most PILOT_MAX_USDT.
D1 is untested: no backtest, paper or live result exists for it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.core.config import AppConfig
from bot.core.money import ZERO, D, floor_price, floor_to_step, fmt, pct, plain
from bot.daily_signal import MarketData, account, corr_group
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import INTERVAL_MS
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, Decision
from bot.strategies.base import entry_limit
from bot.strategies.daily_pullback import (ATR_N, MAX_STOP_PCT, PULLBACK_CANDLES, SLOPE_DAYS, TAKE_PROFIT_R,
                                          DailyPullback, Verdict, ema)

PILOT_MAX_USDT = Decimal("20")         # Sultan's pilot allocation: never more, never raised automatically
PILOT_MAX_RISK_PCT = Decimal("0.5")    # his maximum risk per trade, fees and slippage included
LOG_FILE = "plans.jsonl"


def loss_per_unit(entry: Decimal, stop: Decimal, symbol: str, cfg: AppConfig, fee: Decimal) -> Decimal:
    """USDT lost per coin if the stop fills at its slippage allowance, fees included: the risk engine's own
    formula (step 9 of `evaluate()`). Used only to explain; sizing is always `evaluate()`'s."""
    pe = entry * (1 + pct(cfg.risk.entry_slippage_pct))
    ps = stop * (1 - pct(cfg.risk.stop_slippage_for(symbol)))
    return (pe - ps) + fee * (pe + ps)


@dataclass(frozen=True)
class Feasibility:
    """The smallest order Binance accepts at `entry` with this stop, and the equity each limit needs for it."""
    symbol: str
    entry: Decimal
    stop: Decimal
    qty: Decimal                       # smallest quantity on the step size that meets minQty and minNotional
    notional: Decimal
    worst_loss: Decimal                # at the stop, with the engine's slippage allowance and fees
    needed: dict[str, Decimal]         # limit -> equity it needs for this order
    equity: Decimal

    @property
    def needed_equity(self) -> Decimal:
        return max(v for k, v in self.needed.items() if k != "risk_max")

    @property
    def binding(self) -> str:
        return max((k for k in self.needed if k != "risk_max"), key=lambda k: self.needed[k])

    @property
    def viable(self) -> bool:
        return self.equity >= self.needed_equity

    @property
    def risk_pct(self) -> Decimal:
        return self.worst_loss / self.equity * 100


LIMIT_NAMES = {"risk": "risk per trade (R-01, configured)", "risk_max": f"risk per trade at {PILOT_MAX_RISK_PCT}%",
               "coin": "one coin (R-07)", "invested": "capital invested (R-06)", "cash": "cash incl. the fee"}


def smallest_order(rules: SymbolRules, entry: Decimal) -> Decimal:
    """The smallest quantity on the step size with qty >= minQty and qty x entry >= minNotional."""
    step = rules.step_size
    want = max(rules.min_qty, rules.min_notional / entry if entry > 0 else ZERO)
    q = floor_to_step(want, step)
    return q if q >= want else q + step


def feasibility(symbol: str, entry: Decimal, stop: Decimal, rules: SymbolRules, cfg: AppConfig, fee: Decimal,
                equity: Decimal) -> Feasibility:
    r = cfg.risk
    entry, stop = floor_price(entry, rules.tick_size), floor_price(stop, rules.tick_size)   # as evaluate()
    q = smallest_order(rules, entry)
    pe = entry * (1 + pct(r.entry_slippage_pct))
    worst = q * loss_per_unit(entry, stop, symbol, cfg, fee)
    needed = {"risk": worst / pct(r.risk_per_trade_pct), "risk_max": worst / pct(PILOT_MAX_RISK_PCT),
              "coin": q * pe / pct(r.max_coin_exposure_pct), "invested": q * pe / pct(r.max_invested_pct),
              "cash": q * pe * (1 + fee)}
    return Feasibility(symbol, entry, stop, q, q * entry, worst, needed, equity)


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    at: float
    trade: bool                        # True only when D1 says LONG, the price still allows it and the engine accepts
    reason: str
    price: Decimal | None = None       # best ask now
    verdict: Verdict | None = None
    decision: Decision | None = None
    expires_at: float | None = None
    invalidation: tuple[str, ...] = ()
    fees: Decimal = ZERO               # entry fee + exit fee at the target
    slippage: Decimal = ZERO           # the engine's allowance: entry plus stop
    feasibility: Feasibility | None = None
    notes: list[str] = field(default_factory=list)


def _example_stop(strategy: DailyPullback, cs: list, entry: Decimal) -> Decimal | None:
    """Where D1's stop would be if a setup appeared now (its own rule: stop_atr x 4h ATR below the entry)."""
    a = atr(cs[-ATR_N * 3:], ATR_N)[-1] if len(cs) > ATR_N else None
    return None if a is None or a <= 0 else entry - strategy.stop_atr * a


def plan_coin(strategy: DailyPullback, data: MarketData, symbol: str, cfg: AppConfig, state: AccountState,
              now: float) -> TradePlan:
    fee = state.taker_fee_rate
    try:
        cs = data.closed_candles(symbol, strategy.interval, limit=strategy.history_needed())
    except Exception as e:                                       # market data down: no trade, say why
        return TradePlan(symbol, now, False, f"market data unavailable ({type(e).__name__}: {e})")
    v = strategy.signal(cs, now)
    try:
        rules, snap = data.rules(symbol), data.snapshot(symbol)
    except Exception as e:
        return TradePlan(symbol, now, False, f"could not read the exchange rules or prices ({type(e).__name__}: {e})",
                         verdict=v)
    price = snap.best_ask
    if v.long and v.entry is not None and v.stop is not None:
        entry, stop = v.entry, v.stop
    else:
        entry = entry_limit(cs[-1].close) if cs else price
        stop = _example_stop(strategy, cs, entry)
    feas = feasibility(symbol, entry, stop, rules, cfg, fee, state.equity) if stop and 0 < stop < entry else None
    intent = strategy.intent(symbol, v)
    if intent is None:
        return TradePlan(symbol, now, False, v.reason, price, v, feasibility=feas)
    expires = (v.candle_ms + 2 * INTERVAL_MS[strategy.interval]) / 1000 if v.candle_ms is not None else None
    lim, stp = floor_price(v.entry, rules.tick_size), floor_price(v.stop, rules.tick_size)
    invalid = (f"the best ask is above the entry limit {plain(lim)} (a FOK order would not fill; no chasing)",
               f"the price trades at or below the stop {plain(stp)} before the entry",
               f"the daily close falls below its {strategy.trend_days}-day EMA",
               "a newer 4h candle closes (a new plan replaces this one)",
               "the risk engine rejects it with the account as it is at that moment")
    if expires is not None and now >= expires:
        return TradePlan(symbol, now, False, "the plan has expired (a newer 4h candle has closed)", price, v,
                         expires_at=expires, invalidation=invalid, feasibility=feas)
    if price > lim:
        return TradePlan(symbol, now, False, f"D1 would buy, but the best ask {plain(price)} is already above the "
                         f"entry limit {plain(lim)}: no chasing", price, v, expires_at=expires,
                         invalidation=invalid, feasibility=feas)
    d = evaluate(intent, state, snap, rules, cfg, corr_group(symbol))
    if not d.accepted:
        return TradePlan(symbol, now, False, f"D1 would buy, but the risk engine says no ({d.code.value}): {d.reason}",
                         price, v, d, expires, invalid, feasibility=feas)
    r = cfg.risk
    entry, stop, target = (D(d.details[k]) for k in ("entry", "stop", "take_profit"))   # rounded to the tick
    pe = entry * (1 + pct(r.entry_slippage_pct))
    ps = stop * (1 - pct(r.stop_slippage_for(symbol)))
    fees = fee * d.qty * (pe + target)
    slip = d.qty * ((pe - entry) + (stop - ps))
    return TradePlan(symbol, now, True, v.reason, price, v, d, expires, invalid, fees, slip, feas)


def plan(strategy: DailyPullback, data: MarketData, cfg: AppConfig, equity: Decimal, now: float,
         loss_today: Decimal = ZERO, fee: Decimal = Decimal("0.001")) -> list[TradePlan]:
    """One plan per coin for a pilot account of `equity` USDT (at most PILOT_MAX_USDT) with nothing open."""
    if not 0 < D(equity) <= PILOT_MAX_USDT:
        raise ValueError(f"the pilot equity must be above 0 and at most {PILOT_MAX_USDT} USDT")
    state = account(D(equity), D(loss_today), D(fee))
    return [plan_coin(strategy, data, s, cfg, state, now) for s in strategy.symbols]


def _when(t: float | None) -> str:
    return "-" if t is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _feas_lines(f: Feasibility, cfg: AppConfig) -> list[str]:
    L = [f"  Smallest valid order: {plain(f.qty)} = {fmt(f.notional)} USDT at {plain(f.entry)} with the stop at "
         f"{plain(f.stop)}; it would lose {fmt(f.worst_loss)} USDT at the stop ({f.risk_pct:.2f}% of "
         f"{fmt(f.equity)} USDT, fees and slippage included)",
         "  Equity this order needs, per limit:"]
    for k, v in f.needed.items():
        mark = "  <- binding" if k == f.binding else ""
        L.append(f"    {LIMIT_NAMES[k]:<36} {fmt(v)} USDT{mark}")
    if f.viable:
        L.append(f"  Feasible: {fmt(f.equity)} USDT is enough for the smallest order (the engine still decides).")
    else:
        L.append(f"  NOT feasible: {fmt(f.equity)} USDT is below the {fmt(f.needed_equity)} USDT that the "
                 f"{LIMIT_NAMES[f.binding]} limit needs. The limits are not raised to fit.")
    return L


def render(plans: list[TradePlan], equity: Decimal, loss_today: Decimal, now: float, cfg: AppConfig) -> str:
    L = [f"DailyTradePlanner (D1 rules, Spot, long-only), {_when(now)}",
         f"Pilot account: {fmt(equity)} USDT (at most {PILOT_MAX_USDT}), {fmt(loss_today)} USDT lost today; risk "
         f"per trade {cfg.risk.risk_per_trade_pct}% (max {PILOT_MAX_RISK_PCT}%). READ-ONLY: nothing is sent to "
         f"Binance.", ""]
    for p in plans:
        price = f" (best ask {plain(p.price)})" if p.price is not None else ""
        if not p.trade:
            L += [f"{p.symbol}: NO TRADE{price}", f"  Reason: {p.reason}"]
        else:
            d = p.decision
            entry, stop, target = (D(d.details[k]) for k in ("entry", "stop", "take_profit"))
            L += [f"{p.symbol}: TRADE PLAN{price}",
                  f"  Entry .......... BUY LIMIT FOK at {plain(entry)} (fills at once in full, or nothing)",
                  f"  Stop-loss ...... {plain(stop)} ({fmt((entry - stop) / entry * 100)}% below)",
                  f"  Take-profit .... {plain(target)}",
                  f"  Size ........... {plain(d.qty)} ({fmt(d.qty * entry)} USDT)",
                  f"  Fees (est.) .... {fmt(p.fees)} USDT (entry, and exit at the target)",
                  f"  Slippage ....... {fmt(p.slippage)} USDT allowed (entry and stop)",
                  f"  Max loss ....... {fmt(d.planned_risk)} USDT ({d.planned_risk / equity * 100:.2f}% of equity), "
                  f"fees and slippage included",
                  f"  Net reward/risk  {d.reward_risk:.2f} after slippage and fees",
                  f"  Expires ........ {_when(p.expires_at)}",
                  f"  Reason ......... {p.reason}"]
        if p.invalidation:
            L.append("  Invalid if ..... " + "; ".join(p.invalidation))
        if p.feasibility is not None:
            L += _feas_lines(p.feasibility, cfg)
        L.append("")
    L += ["Every limit is the risk engine's own, unchanged. D1 is untested: no backtest, paper or live result exists."]
    return "\n".join(L) + "\n"


def log_row(p: TradePlan, equity: Decimal) -> dict[str, Any]:
    row: dict[str, Any] = {"at": p.at, "symbol": p.symbol, "plan": "TRADE" if p.trade else "NO TRADE",
                           "reason": p.reason, "equity": str(equity)}
    if p.price is not None:
        row["best_ask"] = str(p.price)
    if p.trade:
        d = p.decision
        row.update(entry=d.details["entry"], stop=d.details["stop"], take_profit=d.details["take_profit"],
                   qty=str(d.qty),
                   max_loss=str(d.planned_risk), reward_risk=str(d.reward_risk), fees=str(p.fees),
                   slippage=str(p.slippage), expires_at=p.expires_at)
    if p.feasibility is not None:
        f = p.feasibility
        row["feasibility"] = {"qty": str(f.qty), "notional": str(f.notional), "worst_loss": str(f.worst_loss),
                              "needed_equity": str(f.needed_equity), "binding": f.binding, "viable": f.viable}
    return row


def record(log_dir: Path, plans: list[TradePlan], equity: Decimal) -> Path:
    """Append one line per coin to the plan log (its own folder)."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = log_dir / LOG_FILE
    with open(path, "a") as f:
        for p in plans:
            f.write(json.dumps(log_row(p, equity)) + "\n")
    return path


# ---------------------------------------------------------------- the watchlist: setups that have not triggered yet
READY, WATCHING, NOT_TRADEABLE, NO_DATA = "READY", "WATCHING", "NOT TRADEABLE", "NO DATA"
CHECK_EQUITY = Decimal("1000")     # a hypothetical account, used only for the checks that do not depend on size


@dataclass(frozen=True)
class Watch:
    """One coin's D1 setup ahead of time: which of D1's conditions hold, the price levels still needed, and the
    plan it would give if it triggered at the current levels (a projection, never an order)."""
    symbol: str
    at: float
    status: str
    reason: str
    price: Decimal | None = None
    met: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    entry: Decimal | None = None       # projected (READY: the real plan's)
    stop: Decimal | None = None
    target: Decimal | None = None
    watch_until: float | None = None   # the next 4h close: the levels are recomputed then
    check: Decision | None = None      # the engine on a hypothetical CHECK_EQUITY account (reward-to-risk etc.)
    pilot: Decision | None = None      # the engine on the pilot account
    feasibility: Feasibility | None = None


def trend_close_needed(strategy: DailyPullback, closes: list[Decimal]) -> Decimal | None:
    """The lowest next daily close that makes D1's uptrend true (close above its EMA, EMA higher than SLOPE_DAYS
    days earlier). Exact for D1's fixed-window EMA: the new close enters only its last step, with weight k."""
    n = strategy.trend_days
    e = ema(closes + [ZERO], n)                        # the earlier values do not depend on the new close
    if len(e) < SLOPE_DAYS + 2:
        return None
    k = D(2) / (n + 1)
    prev, earlier = e[-2], e[-1 - SLOPE_DAYS]
    return max(prev, prev + (earlier - prev) / k)


def _next_close(now: float, ms: int) -> float:
    t = int(now * 1000)
    return (t - t % ms + ms) / 1000


def watch_coin(strategy: DailyPullback, data: MarketData, symbol: str, cfg: AppConfig, state: AccountState,
               now: float) -> Watch:
    p = plan_coin(strategy, data, symbol, cfg, state, now)
    if p.trade:
        d = p.decision
        return Watch(symbol, now, READY, p.reason, p.price, entry=D(d.details["entry"]), stop=D(d.details["stop"]),
                     target=D(d.details["take_profit"]), watch_until=p.expires_at, pilot=d,
                     feasibility=p.feasibility)
    try:
        cs = data.closed_candles(symbol, strategy.interval, limit=strategy.history_needed())
        rules, snap = data.rules(symbol), data.snapshot(symbol)
    except Exception as e:
        return Watch(symbol, now, NO_DATA, f"market data unavailable ({type(e).__name__}: {e})")
    if len(cs) < strategy.history_needed() - 6:
        return Watch(symbol, now, NO_DATA, "not enough 4h history", snap.best_ask)
    four = INTERVAL_MS[strategy.interval]
    until = _next_close(now, four)
    met, needs = [], []
    up, why = strategy.uptrend(daily(cs))
    if up:
        met.append(why)
    else:
        c = trend_close_needed(strategy, [d.close for d in daily(cs)])
        if c is None:
            return Watch(symbol, now, NO_DATA, "not enough daily history", snap.best_ask)
        needs.append(f"a daily close above {plain(ceil_tick(c, rules.tick_size))} at "
                     f"{_when(_next_close(now, DAY_MS))} or later (now: {why})")
    e4 = ema([c.close for c in cs], strategy.pullback_ema)
    touched = [c for c, lvl in zip(cs[-PULLBACK_CANDLES:], e4[-PULLBACK_CANDLES:]) if c.low <= lvl]
    if touched:
        met.append(f"a 4h low touched the {strategy.pullback_ema}-EMA at {_when(touched[-1].open_time / 1000)}")
    else:
        needs.append(f"a 4h low at or below the 4h {strategy.pullback_ema}-EMA (now {plain(ceil_tick(e4[-1], rules.tick_size))})")
    trigger = cs[-1].high
    needs.append(f"then a 4h close above {plain(trigger)} (the last 4h high; this level is reset at every 4h close)")
    a = atr(cs[-ATR_N * 3:], ATR_N)[-1]
    if a is None or a <= 0:
        return Watch(symbol, now, NO_DATA, "no valid 4h ATR", snap.best_ask, tuple(met), tuple(needs))
    entry = entry_limit(trigger)
    stop = entry - strategy.stop_atr * a
    target = entry + TAKE_PROFIT_R * (entry - stop)
    entry, stop, target = (floor_price(x, rules.tick_size) for x in (entry, stop, target))   # as evaluate()
    feas = feasibility(symbol, entry, stop, rules, cfg, state.taker_fee_rate, state.equity)
    base = dict(price=snap.best_ask, met=tuple(met), needs=tuple(needs), entry=entry, stop=stop, target=target,
                watch_until=until, feasibility=feas)
    if (entry - stop) / entry > pct(MAX_STOP_PCT):
        return Watch(symbol, now, NOT_TRADEABLE, f"D1's stop would be more than {MAX_STOP_PCT}% below the entry", **base)
    intent = strategy.intent(symbol, Verdict(True, "projected", entry, stop, target, a))
    check = evaluate(intent, account(CHECK_EQUITY, ZERO, state.taker_fee_rate), snap, rules, cfg, corr_group(symbol))
    pilot = evaluate(intent, state, snap, rules, cfg, corr_group(symbol))
    base.update(check=check, pilot=pilot)
    if not check.accepted:
        return Watch(symbol, now, NOT_TRADEABLE, f"even if it triggered at these levels the risk engine would refuse "
                     f"it at any size ({check.code.value}): {check.reason}", **base)
    if not pilot.accepted:
        return Watch(symbol, now, NOT_TRADEABLE, f"it would pass on a larger account, but not on the pilot account "
                     f"({pilot.code.value}): {pilot.reason}", **base)
    return Watch(symbol, now, WATCHING, "every check passes at these levels; waiting for the conditions above", **base)


def watch(strategy: DailyPullback, data: MarketData, cfg: AppConfig, equity: Decimal, now: float,
          loss_today: Decimal = ZERO, fee: Decimal = Decimal("0.001")) -> list[Watch]:
    if not 0 < D(equity) <= PILOT_MAX_USDT:
        raise ValueError(f"the pilot equity must be above 0 and at most {PILOT_MAX_USDT} USDT")
    state = account(D(equity), D(loss_today), D(fee))
    return [watch_coin(strategy, data, s, cfg, state, now) for s in strategy.symbols]


def render_watch(ws: list[Watch], equity: Decimal, now: float) -> str:
    L = [f"DailyTradePlanner watchlist (D1 rules, unchanged), {_when(now)}; pilot {fmt(equity)} USDT. READ-ONLY.", "",
         f"{'Coin':<8} {'Status':<14} {'Price':>10} {'Entry':>10} {'Stop':>10} {'Target':>10} {'Size':>10} "
         f"{'Max loss':>9}  Watch until"]
    for w in ws:
        d = w.pilot if w.pilot is not None and w.pilot.accepted else None
        cells = [plain(x) if x is not None else "-" for x in (w.price, w.entry, w.stop, w.target)]
        size = plain(d.qty) if d else (plain(w.feasibility.qty) + "*" if w.feasibility else "-")
        loss = fmt(d.planned_risk) if d else (fmt(w.feasibility.worst_loss) + "*" if w.feasibility else "-")
        L.append(f"{w.symbol:<8} {w.status:<14} {cells[0]:>10} {cells[1]:>10} {cells[2]:>10} {cells[3]:>10} "
                 f"{size:>10} {loss:>9}  {_when(w.watch_until)}")
    L += ["* the smallest order Binance accepts, not a size the risk engine approved", ""]
    for w in ws:
        L += [f"{w.symbol}: {w.status}", f"  Why: {w.reason}"]
        L += [f"  Met:   {m}" for m in w.met] + [f"  Needs: {n}" for n in w.needs]
        if w.check is not None and w.check.reward_risk is not None:
            L.append(f"  Net reward/risk at these levels: {w.check.reward_risk:.2f} (the engine needs "
                     f"at least 1.5)")
        if w.feasibility is not None:
            f = w.feasibility
            L.append(f"  Capital the smallest order needs: {fmt(f.needed_equity)} USDT ({LIMIT_NAMES[f.binding]})")
        L.append("  Entry type: BUY LIMIT fill-or-kill, sent only after the 4h close confirms (not a resting order)")
        L.append("")
    L.append("Projections at the current levels, not orders or predictions. D1 has no backtest, paper or live result.")
    return "\n".join(L) + "\n"


def ceil_tick(value: Decimal, tick: Decimal) -> Decimal:
    """A price level rounded UP to the tick (a threshold the close must exceed)."""
    q = floor_price(value, tick)
    return q if q >= value else q + tick
