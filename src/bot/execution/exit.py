"""Closing an open exchange trade (module 5d): a strategy exit, a time stop, or coins found unprotected.

Exits only reduce risk, so they need no risk-engine decision. The protection (the stop and
take-profit order list) is cancelled first, then what is left of the coins is sold at market under
the trade's own exit client ID. Never two sales: if that ID was used before, the order is looked up
and never re-sent; a sale whose outcome is unknown is passed back as a problem, and reconciliation
settles it. What a protective order already sold is not sold again. Coins below the market minimum
stay in the account as dust (the ledger counts it).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot.core.money import ZERO, floor_to_step, plain
from bot.exchange.binance import ExchangeError
from bot.exchange.filters import SymbolRules
from bot.exchange.trading import BinanceTrader, OrderRejected, OrderRequest, OrderState, OrderStatusUnknown
from bot.execution.ledger import TrackedTrade


@dataclass
class CloseResult:
    closed: bool
    left_qty: Decimal = ZERO               # dust left in the account
    sale: OrderState | None = None
    sold_by_protection: Decimal = ZERO
    problem: str = ""


def close_trade(t: TrackedTrade, trader: BinanceTrader, rules: SymbolRules) -> CloseResult:
    try:
        trader.cancel_list(t.symbol, t.ids["list"])
        legs = [trader.query(t.symbol, t.ids["stop"]), trader.query(t.symbol, t.ids["take-profit"])]
        earlier = trader.query(t.symbol, t.ids["exit"])
    except ExchangeError as e:
        return CloseResult(False, problem=f"could not cancel or check the protection: {e}")
    if any(o is not None and not o.final for o in legs):
        return CloseResult(False, problem="a protective order is still open after the cancel; nothing was sold")
    by_legs = sum((o.executed_qty for o in legs if o is not None), ZERO)
    if earlier is not None:
        if earlier.status == "FILLED":
            return CloseResult(True, max(ZERO, t.received_qty - by_legs - earlier.executed_qty), earlier, by_legs)
        return CloseResult(False, sale=earlier, sold_by_protection=by_legs,
                           problem=f"the closing sale {earlier.client_order_id} exists and is {earlier.status}; "
                                   f"it is never sent twice")
    remaining = max(ZERO, t.received_qty - by_legs)
    qty = floor_to_step(remaining, rules.market_step_size or rules.step_size)
    if qty < (rules.market_min_qty or rules.min_qty) or qty == 0:
        return CloseResult(True, remaining, None, by_legs)              # nothing sellable left: dust
    try:
        sale = trader.place(OrderRequest(t.symbol, "SELL", "MARKET", qty, t.ids["exit"]), rules)
    except (OrderRejected, OrderStatusUnknown, ExchangeError) as e:
        return CloseResult(False, sold_by_protection=by_legs,
                           problem=f"the closing sale of {plain(qty)} {t.symbol} failed: {e}")
    if sale.status != "FILLED":
        return CloseResult(False, sale=sale, sold_by_protection=by_legs,
                           problem=f"the closing sale ended {sale.status} ({plain(sale.executed_qty)} sold)")
    return CloseResult(True, max(ZERO, remaining - sale.executed_qty), sale, by_legs)
