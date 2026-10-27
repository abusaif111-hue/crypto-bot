"""Closing an open exchange trade (module 5d): a strategy exit, a time stop, or coins found unprotected.

Exits only reduce risk, so they need no risk-engine decision. The protection (the stop and
take-profit order list) is cancelled first, then what is left of the coins is sold at market.
What a protective order already sold is not sold again. Coins below the market minimum stay in
the account as dust (the ledger counts it).

Never the same coins sold twice (PR-2, audit H2): Binance accepts a repeated client order ID once the
first order has filled, so a sale is never re-sent under an ID used before. Each attempt has its own
ID (exit leg k) and is recorded in the ledger (`record_attempt`) BEFORE it is sent. A new attempt is
sent only when every earlier one is known:
- found and finished with coins left (expired, cancelled, partly filled; audit M1): the next attempt
  sells only what is left;
- not found, recorded less than EXIT_SETTLE_S ago: wait; nothing is sent;
- not found after that, and Binance still holds the coins: it counts as never placed;
- not found after that, and the coins are gone: nothing is sent; the operator must look.
At most MAX_EXIT_ATTEMPTS per trade; then the coins must be sold by hand.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from bot.core.money import ZERO, floor_to_step, plain
from bot.exchange.binance import ExchangeError
from bot.exchange.filters import SymbolRules
from bot.exchange.trading import BinanceTrader, OrderRejected, OrderRequest, OrderState, OrderStatusUnknown
from bot.execution.ledger import EXIT_SETTLE_S, MAX_EXIT_ATTEMPTS, TrackedTrade

RecordAttempt = Callable[[int], None]      # records "sale attempt k is about to be sent" and saves; raises if it can't


@dataclass
class CloseResult:
    closed: bool
    left_qty: Decimal = ZERO               # dust left in the account
    sale: OrderState | None = None         # the latest sale found or sent
    sold_by_protection: Decimal = ZERO
    problem: str = ""
    waiting: bool = False                  # a sale's outcome is not known yet: nothing was sent; try again later
    attempt: int | None = None             # the sale attempt this result is about


def close_trade(t: TrackedTrade, trader: BinanceTrader, rules: SymbolRules, now: float,
                record_attempt: RecordAttempt, held_elsewhere: Decimal) -> CloseResult:
    """Cancel the protection and sell what is left, once. `held_elsewhere`: the coins of this asset the ledger
    expects in the account apart from this trade (the baseline and other trades)."""
    try:
        trader.cancel_list(t.symbol, t.ids["list"])
        legs = [trader.query(t.symbol, t.ids["stop"]), trader.query(t.symbol, t.ids["take-profit"])]
        sales = [trader.query(t.symbol, cid) for cid in t.exit_ids()]
    except ExchangeError as e:
        return CloseResult(False, problem=f"could not cancel or check the protection: {e}")
    if any(o is not None and not o.final for o in legs):
        return CloseResult(False, problem="a protective order is still open after the cancel; nothing was sold")
    by_legs = sum((o.executed_qty for o in legs if o is not None), ZERO)
    found = [k for k, o in enumerate(sales) if o is not None]
    latest = sales[found[-1]] if found else None
    still = next((o for o in sales if o is not None and not o.final), None)
    if still is not None:
        return CloseResult(False, sale=still, sold_by_protection=by_legs, waiting=True,
                           attempt=t.exit_ids().index(still.client_order_id),
                           problem=f"the closing sale {still.client_order_id} is still {still.status}; nothing more "
                                   f"is sent until it ends")
    remaining = max(ZERO, t.received_qty - by_legs - sum((o.executed_qty for o in sales if o is not None), ZERO))
    step = rules.market_step_size or rules.step_size
    qty = floor_to_step(remaining, step)
    if qty == 0 or qty < (rules.market_min_qty or rules.min_qty):
        return CloseResult(True, remaining, latest, by_legs)            # nothing sellable left: dust
    made = max(t.exit_attempts, found[-1] + 1 if found else 0)          # also a sale sent before attempts were kept
    if made and sales[made - 1] is None:                                # the latest attempt cannot be found
        cid = t.exit_ids()[made - 1]
        if now - t.exit_sent_at < EXIT_SETTLE_S:
            return CloseResult(False, sold_by_protection=by_legs, waiting=True, attempt=made - 1,
                               problem=f"the closing sale {cid} cannot be found yet; waiting {EXIT_SETTLE_S:.0f} s "
                                       f"before deciding, and nothing is sent meanwhile")
        try:
            have = next((b.total for b in trader.account().balances if b.asset == t.base_asset), ZERO)
        except ExchangeError as e:
            return CloseResult(False, sold_by_protection=by_legs, attempt=made - 1,
                               problem=f"the closing sale {cid} cannot be found and the balance could not be read "
                                       f"({e}); nothing is sent")
        if have - held_elsewhere < qty - step:
            return CloseResult(False, sold_by_protection=by_legs, attempt=made - 1,
                               problem=f"the closing sale {cid} cannot be found, and Binance holds only "
                                       f"{plain(max(ZERO, have - held_elsewhere))} of the {plain(qty)} {t.base_asset} "
                                       f"still to sell: it may have been executed. Nothing is sent; check the "
                                       f"account by hand")
    if made >= MAX_EXIT_ATTEMPTS:
        return CloseResult(False, sale=latest, sold_by_protection=by_legs,
                           problem=f"{MAX_EXIT_ATTEMPTS} sales were tried and {plain(qty)} {t.base_asset} are still "
                                   f"unsold; sell them by hand")
    cid = t.exit_ids()[made]
    try:
        record_attempt(made)                                            # written before it is sent
    except Exception as e:
        return CloseResult(False, sold_by_protection=by_legs,
                           problem=f"could not record the sale before sending it ({type(e).__name__}: {e}); nothing "
                                   f"was sent")
    try:
        sale = trader.place(OrderRequest(t.symbol, "SELL", "MARKET", qty, cid), rules)
    except (OrderRejected, OrderStatusUnknown, ExchangeError) as e:
        return CloseResult(False, sold_by_protection=by_legs, attempt=made,
                           problem=f"the closing sale {cid} of {plain(qty)} {t.symbol} failed: {e}")
    left = max(ZERO, remaining - sale.executed_qty)
    if sale.status != "FILLED":
        return CloseResult(False, sale=sale, sold_by_protection=by_legs, attempt=made,
                           problem=f"the closing sale {cid} ended {sale.status} ({plain(sale.executed_qty)} sold); "
                                   f"what is left is sold by the next attempt")
    return CloseResult(True, left, sale, by_legs, attempt=made)
