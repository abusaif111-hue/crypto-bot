"""Testnet round trips (module 5b, toward the Stage 5 gate: 5 clean round trips).

One round trip: a protected entry through `entry.submit()` (risk engine first), a short hold,
then the close: cancel the protection and sell the coins received at market, and confirm that
nothing of this trade is left open. It is clean when the protection was confirmed, the close
filled, and both protective orders ended cancelled or filled (one filled only if the stop or
take-profit closed the position during the hold). Every attempt is appended to a journal; only
clean ones count. Testnet only: `BinanceTrader` refuses any other environment.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.core.ids import client_order_id
from bot.core.money import ZERO, floor_to_step
from bot.exchange.binance import ExchangeError
from bot.exchange.filters import SymbolRules
from bot.exchange.trading import BinanceTrader, OrderRejected, OrderRequest, OrderState, OrderStatusUnknown
from bot.execution.entry import PROTECTED, EntryOutcome

JOURNAL = "roundtrips.jsonl"
GATE = 5
DONE = ("CANCELED", "FILLED", "EXPIRED")


@dataclass
class RoundTrip:
    entry: EntryOutcome
    close: OrderState | None = None
    stop_after: OrderState | None = None
    take_profit_after: OrderState | None = None
    clean: bool = False
    problems: list[str] = field(default_factory=list)


def close(outcome: EntryOutcome, trader: BinanceTrader, rules: SymbolRules) -> RoundTrip:
    """Cancel the protection, sell what is left at market, and check that nothing stays open."""
    rt = RoundTrip(outcome)
    plan = outcome.plan
    if outcome.status != PROTECTED:
        rt.problems.append(f"entry was {outcome.status}: {outcome.reason}")
        return rt
    try:
        trader.cancel_list(plan.symbol, plan.list_client_id)
        rt.stop_after = trader.query(plan.symbol, plan.stop_client_id)
        rt.take_profit_after = trader.query(plan.symbol, plan.take_profit_client_id)
    except ExchangeError as e:
        rt.problems.append(f"could not cancel or check the protection: {e}")
        return rt
    legs = [o for o in (rt.stop_after, rt.take_profit_after) if o is not None]
    sold = sum((o.executed_qty for o in legs), ZERO)
    left = floor_to_step(outcome.received_qty - sold, rules.market_step_size or rules.step_size)
    if left > 0:
        intent_id = uuid.UUID(hex=plan.entry_client_id[-32:])
        try:
            rt.close = trader.place(OrderRequest(plan.symbol, "SELL", "MARKET", left,
                                                 client_order_id(intent_id, "exit")), rules)
        except (OrderRejected, OrderStatusUnknown, ExchangeError) as e:
            rt.problems.append(f"the closing sale failed: {e}; sell {left} {plan.symbol} by hand")
            return rt
        if rt.close.status != "FILLED":
            rt.problems.append(f"the closing sale ended {rt.close.status}")
    if len(legs) != 2 or any(o.status not in DONE for o in legs):
        rt.problems.append("a protective order is still open or missing after the close")
    if sum(1 for o in legs if o.status == "FILLED") > 1:
        rt.problems.append("both protective orders filled")
    rt.clean = not rt.problems
    return rt


def record(journal_dir: Path, rt: RoundTrip, now: float) -> int:
    """Append the attempt; returns the number of clean round trips so far."""
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    e, plan = rt.entry, rt.entry.plan
    row: dict[str, Any] = {
        "at": now, "clean": rt.clean, "entry_status": e.status, "reason": e.reason, "problems": rt.problems,
        "symbol": plan.symbol if plan else None, "list_client_id": plan.list_client_id if plan else None,
        "qty": str(plan.qty) if plan else None, "entry_price": str(plan.entry_price) if plan else None,
        "stop": str(plan.stop_price) if plan else None, "take_profit": str(plan.take_profit_price) if plan else None,
        "stop_type": plan.stop_type if plan else None, "received": str(e.received_qty),
        "bought_usdt": str(e.entry.quote_qty) if e.entry else None,
        "sold_usdt": str(_sold_usdt(rt)), "fees": {a: str(q) for a, q in fees(rt).items()}, "notes": e.notes}
    with open(journal_dir / JOURNAL, "a") as f:
        f.write(json.dumps(row) + "\n")
    return clean_count(journal_dir)


def avg_price(o: OrderState | None) -> Decimal | None:
    """Average fill price of an order, or None if nothing filled."""
    if o is None or o.executed_qty <= 0:
        return None
    return o.quote_qty / o.executed_qty


def pnl_usdt(rt: RoundTrip) -> Decimal | None:
    """USDT received from the sales less USDT spent on the buy, before fees; None if nothing was bought."""
    e = rt.entry.entry
    if e is None or e.executed_qty <= 0:
        return None
    return _sold_usdt(rt) - e.quote_qty


def fees(rt: RoundTrip) -> dict[str, Decimal]:
    """Fees Binance reported in the fills of the buy and the closing sale, per asset. A protective order that
    filled is looked up without its fills, so its fee is not included."""
    out: dict[str, Decimal] = {}
    for o in (rt.entry.entry, rt.entry.exit, rt.close):
        for f in (o.fills if o is not None else ()):
            out[f.commission_asset] = out.get(f.commission_asset, ZERO) + f.commission
    return out


def _sold_usdt(rt: RoundTrip) -> Decimal:
    orders = [o for o in (rt.close, rt.stop_after, rt.take_profit_after, rt.entry.exit) if o is not None]
    return sum((o.quote_qty for o in orders), ZERO)


def clean_count(journal_dir: Path) -> int:
    path = Path(journal_dir) / JOURNAL
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip() and json.loads(line).get("clean") is True)
