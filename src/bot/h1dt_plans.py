"""Future trade plans for H1-DailyTrader (`bot plan --strategy H1DT`): every setup, every coin, ahead of time.

For each coin and setup (BRK, CONT, RNG; `bot.strategies.daily_trader`, unchanged) on finished 1h candles:
- READY: the setup is on the last finished 1h candle and its 4h context holds: the plan to send at the next
  open (BUY LIMIT fill-or-kill at the last close + 0.10%; a higher price is not chased);
- WATCHING: not triggered yet; the level the next 1h close must reach, which conditions already hold, and the
  plan it would give at that level;
- NOT TRADEABLE: even at that level the risk engine would refuse it at any size (with its reason).
Every plan is checked by `evaluate()` (limits unchanged) twice: on a hypothetical 1,000 USDT account (the
checks that do not depend on size, such as reward-to-risk after costs) and on the pilot account. An order is
eligible only when READY, accepted on the pilot, and the account holds none of the coin (the account is
shared with H2b, so coins already held would overlap).

Read-only: it places no order and is not wired into any runner. Levels are recomputed at every 1h close.
H1-DailyTrader has no backtest, paper or live result yet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from bot.core.config import AppConfig
from bot.core.money import ZERO, D, floor_price, fmt, pct, plain
from bot.daily_signal import MarketData, account, corr_group
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, Decision
from bot.strategies.base import entry_limit
from bot.strategies.daily_trader import (CONT_BREAK, CONT_SLOPE, RANGE_MIN_ATR, RANGE_ZONE, SETUPS, DailyTrader, Setup,
                                         ema)
from bot.trade_planner import (CHECK_EQUITY, LIMIT_NAMES, NO_DATA, NOT_TRADEABLE, PILOT_MAX_USDT, READY, WATCHING,
                               Feasibility, feasibility)

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class Plan:
    symbol: str
    setup: str
    at: float
    status: str
    reason: str
    price: Decimal | None = None
    met: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    watch_until: float | None = None
    invalidation: str = ""
    check: Decision | None = None          # on the hypothetical CHECK_EQUITY account
    pilot: Decision | None = None          # on the pilot account
    feasibility: Feasibility | None = None
    fees: Decimal = ZERO                   # for the smallest valid order: entry, and exit at the target
    slippage: Decimal = ZERO               # the engine's allowance for it: entry and stop
    order: str = ""                        # eligible for an exchange order, or why not


def _next_hour(now: float) -> float:
    t = int(now * 1000)
    return (t - t % HOUR_MS + HOUR_MS) / 1000


def _projection(s: DailyTrader, hours: list) -> tuple[Decimal, Decimal, Decimal, list[str], list[str], str] | None:
    """(entry, stop, target, met, needs, invalidation) if the setup triggered at the next 1h close, at the level
    it needs; None when no level can be computed."""
    a = s._atr(hours)
    if a is None:
        return None
    met, needs = [], []
    if s.setup in ("BRK", "CONT"):
        n = s.lookback if s.setup == "BRK" else CONT_BREAK
        level = max(c.high for c in hours[-n:])
        if s.setup == "CONT":
            e = ema([c.close for c in hours], s.lookback)
            if len(e) > CONT_SLOPE and e[-1] > e[-1 - CONT_SLOPE]:
                met.append(f"the 1h {s.lookback}-EMA is rising ({e[-1]:.2f})")
            else:
                needs.append(f"a rising 1h {s.lookback}-EMA (now {e[-1]:.2f})" if e else "1h EMA history")
            if e and level <= e[-1]:
                needs.append(f"the close above the 1h {s.lookback}-EMA {e[-1]:.2f}")
        needs.append(f"a 1h close above {plain(level)} (the {n}-hour high)")
        entry = entry_limit(level)
        stop = entry - s.stop_atr * a
        invalid = f"a 1h close back below {plain(level)} before the entry"
    else:
        window = hours[-s.lookback:]
        lo, hi = min(c.low for c in window), max(c.high for c in window)
        if hi - lo < RANGE_MIN_ATR * a:
            needs.append(f"a range at least {RANGE_MIN_ATR} x the 1h ATR wide (now {plain(hi - lo)}, "
                         f"needs {plain(RANGE_MIN_ATR * a)})")
        zone = lo + RANGE_ZONE * (hi - lo)
        needs.append(f"a 1h candle dipping to {plain(zone)} or below (range {plain(lo)} to {plain(hi)}) and "
                     f"closing up, above {plain(lo)}")
        entry = entry_limit(zone)
        stop = lo - s.stop_atr * a
        invalid = f"a 1h close below the range low {plain(lo)}"
    if not 0 < stop < entry:
        return None
    target = entry + s.target_r * (entry - stop)
    return entry, stop, target, met, needs, invalid


def plan_setup(s: DailyTrader, data: MarketData, symbol: str, cfg: AppConfig, state: AccountState, now: float,
               held: Decimal = ZERO) -> Plan:
    try:
        hours = data.closed_candles(symbol, s.interval, limit=s.history_needed())
        rules, snap = data.rules(symbol), data.snapshot(symbol)
    except Exception as e:
        return Plan(symbol, s.setup, now, NO_DATA, f"market data unavailable ({type(e).__name__}: {e})")
    if len(hours) < s.history_needed() - 4:
        return Plan(symbol, s.setup, now, NO_DATA, "not enough 1h history", snap.best_ask)
    ctx_ok, ctx_why = s.context(hours)
    found = s.trigger(hours)
    if found is not None and ctx_ok:
        entry, stop, target, invalid = found.entry, found.stop, found.target, found.invalidation
        met, needs, status_if_ok, why = (ctx_why,), (), READY, found.reason
        until = _next_hour(now)                          # sent at the next open, or not at all
        if snap.best_ask > floor_price(entry, rules.tick_size):
            return Plan(symbol, s.setup, now, NOT_TRADEABLE, f"triggered, but the best ask {plain(snap.best_ask)} is "
                        f"already above the entry limit {plain(floor_price(entry, rules.tick_size))}: no chasing",
                        snap.best_ask, met, needs, watch_until=until, invalidation=invalid)
    else:
        p = _projection(s, hours)
        if p is None:
            return Plan(symbol, s.setup, now, NO_DATA, "no valid 1h ATR or levels", snap.best_ask)
        entry, stop, target, met, needs, invalid = p
        met = [*([ctx_why] if ctx_ok else []), *met]
        needs = [*([] if ctx_ok else [f"the 4h context: {ctx_why}"]), *needs]
        met, needs, status_if_ok, why = tuple(met), tuple(needs), WATCHING, "not triggered yet"
        until = _next_hour(now)
    entry, stop, target = (floor_price(x, rules.tick_size) for x in (entry, stop, target))
    intent = s.intent(symbol, Setup(entry, stop, target, ZERO, why, invalid))
    fee = state.taker_fee_rate
    check = evaluate(intent, account(CHECK_EQUITY, ZERO, fee), snap, rules, cfg, corr_group(symbol))
    pilot = evaluate(intent, state, snap, rules, cfg, corr_group(symbol))
    feas = feasibility(symbol, entry, stop, rules, cfg, fee, state.equity)
    pe = entry * (1 + pct(cfg.risk.entry_slippage_pct))
    ps = stop * (1 - pct(cfg.risk.stop_slippage_for(symbol)))
    fees = fee * feas.qty * (pe + target)
    slip = feas.qty * ((pe - entry) + (stop - ps))
    if not check.accepted:
        status, why = NOT_TRADEABLE, (f"the risk engine would refuse it at any size ({check.code.value}): "
                                      f"{check.reason}")
    else:
        status = status_if_ok
    if status != READY:
        order = "no: not triggered" if status == WATCHING else "no"
    elif not pilot.accepted:
        order = f"no: the pilot account is too small ({pilot.code.value}); needs {fmt(feas.needed_equity)} USDT"
    elif held > 0:
        order = f"no: the account already holds {plain(held)} {rules.base_asset} (shared with H2b)"
    else:
        order = "yes (BUY LIMIT FOK at the next open; your approval and the live switch are still needed)"
    return Plan(symbol, s.setup, now, status, why, snap.best_ask, tuple(met), tuple(needs), entry, stop, target,
                until, invalid, check, pilot, feas, fees, slip, order)


def plans(data: MarketData, cfg: AppConfig, equity: Decimal, now: float, loss_today: Decimal = ZERO,
          fee: Decimal = Decimal("0.001"), held: dict[str, Decimal] | None = None) -> list[Plan]:
    if not 0 < D(equity) <= PILOT_MAX_USDT:
        raise ValueError(f"the pilot equity must be above 0 and at most {PILOT_MAX_USDT} USDT")
    state = account(D(equity), D(loss_today), D(fee))
    held = held or {}
    out = []
    for sym in cfg.universe:
        for setup in SETUPS:
            out.append(plan_setup(DailyTrader([sym], setup), data, sym, cfg, state, now,
                                  held.get(sym[:-4], ZERO)))
    return out


def _when(t: float | None) -> str:
    return "-" if t is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def render(ps: list[Plan], equity: Decimal, now: float) -> str:
    L = [f"H1-DailyTrader future setups (1h, 4h context), {_when(now)}; pilot {fmt(equity)} USDT. READ-ONLY.", "",
         f"{'Coin':<8} {'Setup':<5} {'Status':<14} {'Price':>10} {'Entry':>10} {'Stop':>10} {'Target':>10} "
         f"{'Net R:R':>7} {'Min size':>9} {'Max loss':>9}  Watch until"]
    for p in ps:
        rr = "-"
        if p.check is not None:
            v = p.check.reward_risk if p.check.reward_risk is not None else p.check.details.get("reward_risk")
            rr = "-" if v is None else f"{D(v):.2f}"                     # a refusal carries it in its details
        cells = [plain(x) if x is not None else "-" for x in (p.price, p.entry, p.stop, p.target)]
        size = plain(p.feasibility.qty) if p.feasibility else "-"
        loss = fmt(p.feasibility.worst_loss) if p.feasibility else "-"
        L.append(f"{p.symbol:<8} {p.setup:<5} {p.status:<14} {cells[0]:>10} {cells[1]:>10} {cells[2]:>10} "
                 f"{cells[3]:>10} {rr:>7} {size:>9} {loss:>9}  {_when(p.watch_until)}")
    L += ["Min size: the smallest order Binance accepts at that entry; Max loss: what it loses at the stop with the",
          "engine's slippage and fees.", ""]
    for p in ps:
        L += [f"{p.symbol} {p.setup}: {p.status}", f"  Why: {p.reason}"]
        L += [f"  Met:   {m}" for m in p.met] + [f"  Needs: {n}" for n in p.needs]
        if p.invalidation:
            L.append(f"  Invalid if: {p.invalidation}")
        if p.feasibility is not None:
            f = p.feasibility
            L.append(f"  Fees (est.) {fmt(p.fees)} USDT; slippage allowed {fmt(p.slippage)} USDT; capital the "
                     f"smallest order needs {fmt(f.needed_equity)} USDT ({LIMIT_NAMES[f.binding]})")
        if p.pilot is not None:
            L.append(f"  Pilot account: {'accepted' if p.pilot.accepted else 'refused'}"
                     f"{'' if p.pilot.accepted else f' ({p.pilot.code.value}): {p.pilot.reason}'}")
        if p.order:
            L.append(f"  Exchange order: {p.order}")
        L.append("")
    L.append("Projections and plans, not orders. H1-DailyTrader has no backtest, paper or live result yet.")
    return "\n".join(L) + "\n"
