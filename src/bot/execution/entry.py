"""Protected entries (module 5b): the only way the bot opens a position on an exchange.

`submit()` runs the risk engine itself (as `SimExchange.submit()` does): no caller can hand it a
ready-made decision. An accepted idea becomes one OPOCO order list (`bot.exchange.trading`): a
fill-or-kill BUY at the engine's entry price, and on a full fill a SELL take-profit and a SELL
stop-loss for the coins received.

After a fill, both protective orders are looked up by their own IDs and must be open, on the
SELL side, at the planned prices and for at least the coins received (less a rounding step).
If they cannot be confirmed, the protection is cancelled, the coins are sold at market, and
the outcome tells the caller to block new entries. An order whose outcome is unknown is never
re-sent; `OrderStatusUnknown` is passed on, and the caller must block new entries.

Prices are the risk engine's own, already on Binance's tick: entry and stop rounded down (a
lower stop means a smaller position), take-profit rounded down. Where Binance offers no market
STOP_LOSS for the coin, a STOP_LOSS_LIMIT is used with its limit at the stop minus the coin's
stop-slippage allowance, the loss the engine already sized for. Verify on testnet: OPOCO with a
FOK working order, and how quickly the protective orders appear after the fill.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from bot.core.config import AppConfig
from bot.core.ids import client_order_id
from bot.core.money import ZERO, D, floor_price, floor_to_step, pct
from bot.exchange.binance import ExchangeError
from bot.exchange.filters import SymbolRules
from bot.exchange.trading import (BinanceTrader, OrderRejected, OrderRequest, OrderState, OrderStatusUnknown,
                                  ProtectedEntryRequest)
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, Decision, MarketSnapshot, TradeIntent

PROTECTED = "PROTECTED"            # filled, stop and take-profit confirmed open
NOT_FILLED = "NOT_FILLED"          # fill-or-kill expired: nothing bought, nothing open
REJECTED = "REJECTED"              # the risk engine said no
REFUSED = "REFUSED"                # the order was not valid for Binance, or Binance refused it; nothing open
EMERGENCY_EXIT = "EMERGENCY_EXIT"  # filled but protection not confirmed: sold at market; block new entries
UNPROTECTED = "UNPROTECTED"        # filled, protection not confirmed and the emergency sale failed: act now


@dataclass
class EntryOutcome:
    status: str
    reason: str
    decision: Decision | None = None
    plan: ProtectedEntryRequest | None = None
    entry: OrderState | None = None
    stop: OrderState | None = None
    take_profit: OrderState | None = None
    exit: OrderState | None = None
    received_qty: Decimal = ZERO
    block_entries: bool = False
    notes: list[str] = field(default_factory=list)


def stop_order_type(rules: SymbolRules) -> str | None:
    if "STOP_LOSS" in rules.order_types:
        return "STOP_LOSS"
    if "STOP_LOSS_LIMIT" in rules.order_types:
        return "STOP_LOSS_LIMIT"
    return None


def plan_entry(decision: Decision, rules: SymbolRules, cfg: AppConfig, intent_id: uuid.UUID) -> ProtectedEntryRequest:
    """The order list for an accepted decision. Prices are the engine's (already on the tick)."""
    if not decision.accepted:
        raise ValueError("only an accepted decision can be planned")
    d = decision.details
    stop = D(d["stop"])
    stop_type = stop_order_type(rules)
    limit = None
    if stop_type == "STOP_LOSS_LIMIT":
        limit = floor_price(stop * (1 - pct(cfg.risk.stop_slippage_for(rules.symbol))), rules.tick_size)
    return ProtectedEntryRequest(
        symbol=rules.symbol, qty=floor_to_step(decision.qty, rules.step_size), entry_price=D(d["entry"]),
        stop_price=stop, take_profit_price=D(d["take_profit"]), stop_type=stop_type or "NONE",
        list_client_id=client_order_id(intent_id, "list"), entry_client_id=client_order_id(intent_id, "entry"),
        stop_client_id=client_order_id(intent_id, "protect", 0),
        take_profit_client_id=client_order_id(intent_id, "protect", 1), stop_limit_price=limit)


def received_qty(entry: OrderState, base_asset: str) -> Decimal:
    """Coins received: the filled quantity less any fee Binance took in the coin itself."""
    fee_in_coin = sum((f.commission for f in entry.fills if f.commission_asset == base_asset), ZERO)
    return entry.executed_qty - fee_in_coin


def submit(intent: TradeIntent, state: AccountState, snapshot: MarketSnapshot, rules: SymbolRules, cfg: AppConfig,
           trader: BinanceTrader, intent_id: uuid.UUID, corr_group: str,
           sleep: Callable[[float], None] = time.sleep, verify_attempts: int = 5,
           verify_wait_s: float = 1.0) -> EntryOutcome:
    """Risk engine, then one protected entry, then confirmation of the protection."""
    decision = evaluate(intent, state, snapshot, rules, cfg, corr_group)
    if not decision.accepted:
        return EntryOutcome(REJECTED, decision.reason, decision)
    plan = plan_entry(decision, rules, cfg, intent_id)
    try:
        entry = trader.place_protected_entry(plan, rules, state.taker_fee_rate)
    except OrderRejected as e:
        return EntryOutcome(REFUSED, str(e), decision, plan)
    out = EntryOutcome("", "", decision, plan, entry)
    if entry.status == "EXPIRED" and entry.executed_qty == 0:
        out.status, out.reason = NOT_FILLED, "fill-or-kill: the price moved away; nothing was bought"
        return out
    if entry.status != "FILLED":
        # Fill-or-kill should never leave a partial or open buy; if it does, treat it as unprotected.
        trader.cancel_list(plan.symbol, plan.list_client_id)
        entry = trader.query(plan.symbol, plan.entry_client_id) or entry
        out.entry = entry
        if entry.executed_qty == 0:
            out.status, out.reason = NOT_FILLED, f"entry ended {entry.status} with nothing bought"
            out.block_entries, out.notes = True, ["a fill-or-kill entry did not end FILLED or EXPIRED; check it"]
            return out
        return _emergency_exit(out, trader, rules, f"entry ended {entry.status} after a partial fill")
    out.received_qty = received_qty(entry, rules.base_asset)
    problem = ""
    for _ in range(verify_attempts):
        problem = _check_protection(out, trader, rules)
        if not problem or problem == "closed":
            break
        sleep(verify_wait_s)
    if problem == "closed":
        out.status, out.reason = PROTECTED, "filled; a protective order has already filled (position closed)"
        return out
    if problem:
        return _emergency_exit(out, trader, rules, f"protection not confirmed: {problem}")
    out.status, out.reason = PROTECTED, "filled; stop-loss and take-profit confirmed open on Binance"
    return out


def _check_protection(out: EntryOutcome, trader: BinanceTrader, rules: SymbolRules) -> str:
    """Empty if both protective orders are open as planned; 'closed' if one already filled; else the problem."""
    plan = out.plan
    try:
        stop = trader.query(plan.symbol, plan.stop_client_id)
        tp = trader.query(plan.symbol, plan.take_profit_client_id)
    except ExchangeError as e:
        return f"could not look the orders up ({e})"
    out.stop, out.take_profit = stop, tp
    if stop is None or tp is None:
        return "a protective order does not exist on Binance"
    if stop.status == "FILLED" or tp.status == "FILLED":
        return "closed"
    if stop.status != "NEW" or tp.status != "NEW":
        return f"protective orders are {stop.status} / {tp.status}, not NEW"
    if stop.side != "SELL" or tp.side != "SELL":
        return "a protective order is not a SELL"
    if stop.type != plan.stop_type or tp.type != "LIMIT_MAKER":
        return f"protective order types are {stop.type} / {tp.type}"
    if stop.stop_price != plan.stop_price or tp.price != plan.take_profit_price:
        return f"protective prices are {stop.stop_price} / {tp.price}, planned {plan.stop_price} / {plan.take_profit_price}"
    needed = out.received_qty - rules.step_size          # Binance rounds the protected amount down to the step
    if stop.orig_qty < needed or tp.orig_qty < needed:
        return f"protective orders cover {stop.orig_qty} / {tp.orig_qty}, received {out.received_qty}"
    return ""


def _emergency_exit(out: EntryOutcome, trader: BinanceTrader, rules: SymbolRules, why: str) -> EntryOutcome:
    """Cancel whatever protection exists and sell the coins at market. Always blocks new entries."""
    plan = out.plan
    out.block_entries = True
    out.notes.append(why)
    try:
        trader.cancel_list(plan.symbol, plan.list_client_id)
    except ExchangeError as e:
        out.notes.append(f"cancelling the protection failed: {e}")
    qty = floor_to_step(out.received_qty or received_qty(out.entry, rules.base_asset),
                        rules.market_step_size or rules.step_size)
    intent_id = uuid.UUID(hex=plan.entry_client_id[-32:])
    sell = OrderRequest(plan.symbol, "SELL", "MARKET", qty, client_order_id(intent_id, "exit"))
    try:
        out.exit = trader.place(sell, rules)
    except (OrderRejected, OrderStatusUnknown, ExchangeError) as e:
        out.status, out.reason = UNPROTECTED, f"{why}; the emergency sale FAILED ({e}): sell {qty} {plan.symbol} by hand"
        return out
    out.status, out.reason = EMERGENCY_EXIT, f"{why}; sold {out.exit.executed_qty} at market"
    return out
