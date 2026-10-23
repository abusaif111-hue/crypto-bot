"""Reconciliation (module 5c): does Binance hold what the bot's ledger says, and nothing else?

Read-only: it looks orders up and reads balances; it never places or cancels anything (enforced
by tests). What it finds goes to the caller, which blocks new entries unless the result is clean
(`faults()` names the mode-controller faults to report).

Checked, every run:
- PENDING trades (sent, outcome not recorded): the entry is looked up by its client ID. No such
  order after SETTLE_S seconds means it was never placed; younger, it stays unsettled. A filled
  entry is checked like an OPEN trade, with the coins received worked out from its fills.
- OPEN trades: both protective orders must be open SELLs covering the coins (less one step). One
  filled means Binance closed the trade (the other must have ended); both gone means the bot's
  own market sale closed it, or, if that sale did not fill, the coins are UNPROTECTED.
- Open orders: every open order on the account must belong to a PENDING or OPEN trade.
- Coins: for each coin in the baseline, Binance's total (free + locked) must equal the ledger's
  expected holding within the coin's tolerance (one step). Skipped while a trade is unsettled.
  USDT is not compared: it moves with prices, fees and P&L.

Updates it is certain of (never placed, opened, closed) are returned for `apply()`; an exchange
error discards them all and fails closed. Fees paid in BNB move the BNB balance: with BNB in the
baseline that shows as a mismatch (fail closed). Verify on testnet: how soon a timed-out order
appears in GET /api/v3/order (SETTLE_S).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from bot.core.money import ZERO, plain
from bot.exchange.binance import ExchangeError
from bot.exchange.trading import AccountInfo, OrderState, TradeFill
from bot.execution.ledger import CLOSED, NOT_OPENED, OPEN, PENDING, Ledger, TrackedTrade

SETTLE_S = 300.0
ENDED = ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")


class ReadOnlyTrader(Protocol):
    """The only trader methods reconciliation may use."""

    def query(self, symbol: str, client_order_id: str) -> OrderState | None: ...
    def open_orders(self, symbol: str | None = None) -> list[OrderState]: ...
    def account(self) -> AccountInfo: ...
    def my_trades(self, symbol: str, order_id: int) -> list[TradeFill]: ...


@dataclass(frozen=True)
class Update:
    intent_id: str
    status: str
    received_qty: Decimal | None = None
    left_qty: Decimal | None = None
    note: str = ""


@dataclass
class Reconciliation:
    mismatches: list[str] = field(default_factory=list)     # something differs: an operator must look
    unprotected: list[str] = field(default_factory=list)    # coins held without confirmed protection: act now
    unsettled: list[str] = field(default_factory=list)      # an order's outcome is not known yet: wait
    errors: list[str] = field(default_factory=list)         # Binance could not be read: try again
    updates: list[Update] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)        # what matched, for the report

    @property
    def clean(self) -> bool:
        return not (self.mismatches or self.unprotected or self.unsettled or self.errors)

    def faults(self) -> list[tuple[str, str]]:
        """(fault, detail) pairs for `ModeController.report_fault`."""
        out = []
        if self.unprotected:
            out.append(("UNPROTECTED_POSITION", "; ".join(self.unprotected)))
        if self.mismatches or self.unsettled:
            out.append(("RECONCILIATION_MISMATCH", "; ".join(self.mismatches + self.unsettled)))
        if self.errors:
            out.append(("EXCHANGE_UNAVAILABLE", "; ".join(self.errors)))
        return out


def reconcile(ledger: Ledger, trader: ReadOnlyTrader, tolerance: dict[str, Decimal], now: float) -> Reconciliation:
    rec = Reconciliation()
    try:
        _check(ledger, trader, tolerance, now, rec)
    except ExchangeError as e:
        return Reconciliation(errors=[f"could not read Binance: {e}"])
    return rec


def apply(ledger: Ledger, rec: Reconciliation, now: float) -> None:
    """Record the certain updates and save. Only for a run without exchange errors."""
    if rec.errors:
        return
    for u in rec.updates:
        ledger.mark(u.intent_id, u.status, now, received_qty=u.received_qty, left_qty=u.left_qty, note=u.note)
    if rec.updates:
        ledger.save()


# ---------------------------------------------------------------- checks
def _check(ledger: Ledger, trader: ReadOnlyTrader, tolerance: dict[str, Decimal], now: float,
           rec: Reconciliation) -> None:
    held: dict[str, Decimal] = {}                     # coins of trades that are OPEN after this run
    for t in list(ledger.active()):
        if t.status == PENDING:
            received = _resolve_pending(t, trader, now, rec)
        else:
            received = t.received_qty
        if received is not None:
            if _check_open(t, received, trader, tolerance.get(t.base_asset, ZERO), rec):
                held[t.base_asset] = held.get(t.base_asset, ZERO) + received

    known = {cid: t for t in ledger.active() for cid in t.ids.values()}
    others = {cid: t for t in ledger.trades.values() if t.status not in (PENDING, OPEN) for cid in t.ids.values()}
    for o in trader.open_orders(None):
        if o.client_order_id in known:
            continue
        what = f"{o.symbol} {o.side} {o.type} {plain(o.orig_qty)} ({o.client_order_id})"
        old = others.get(o.client_order_id)
        rec.mismatches.append(f"open order {what} belongs to trade {old.intent_id[:8]}, which is {old.status}"
                              if old else f"open order {what} is not one of the bot's")
    if not rec.mismatches and not rec.unsettled:
        rec.checked.append("open orders: all belong to the bot's open trades")

    if rec.unsettled:
        rec.checked.append("coins: not compared while an order's outcome is unknown")
        return
    totals = {b.asset: b.total for b in trader.account().balances}
    expected = _expected(ledger, rec, held)
    for asset, want in sorted(expected.items()):
        have = totals.get(asset, ZERO)
        if abs(have - want) > tolerance.get(asset, ZERO):
            rec.mismatches.append(f"{asset}: Binance holds {plain(have)}, the ledger expects {plain(want)} "
                                  f"(difference {plain(have - want)}); if the testnet account was reset, "
                                  f"record a new baseline")
        else:
            rec.checked.append(f"{asset}: {plain(have)} as expected")


def _expected(ledger: Ledger, rec: Reconciliation, held: dict[str, Decimal]) -> dict[str, Decimal]:
    """Baseline + coins still held + dust of closed trades, after this run's updates."""
    final = {u.intent_id: u for u in rec.updates}
    out = dict(ledger.baseline)
    for t in ledger.trades.values():
        u = final.get(t.intent_id)
        status = u.status if u else t.status
        if status == CLOSED:
            left = u.left_qty if u and u.left_qty is not None else t.left_qty
            out[t.base_asset] = out.get(t.base_asset, ZERO) + left
    for asset, qty in held.items():
        out[asset] = out.get(asset, ZERO) + qty
    return out


def _resolve_pending(t: TrackedTrade, trader: ReadOnlyTrader, now: float, rec: Reconciliation) -> Decimal | None:
    """The coins received if the entry filled; None if the trade did not open (or is not settled)."""
    e = trader.query(t.symbol, t.ids["entry"])
    if e is None:
        if now - t.created_at >= SETTLE_S:
            rec.updates.append(Update(t.intent_id, NOT_OPENED, note="Binance has no such entry order"))
        else:
            rec.unsettled.append(f"{t.symbol} entry {t.ids['entry']}: not found yet; waiting "
                                 f"{SETTLE_S:.0f} s before treating it as never placed")
        return None
    if not e.final:
        rec.mismatches.append(f"{t.symbol} entry {e.client_order_id} is {e.status}; a fill-or-kill entry "
                              f"should never stay open")
        return None
    if e.executed_qty == 0:
        rec.updates.append(Update(t.intent_id, NOT_OPENED, note=f"entry {e.status} with nothing bought"))
        return None
    fee = sum((f.commission for f in trader.my_trades(t.symbol, e.order_id) if f.commission_asset == t.base_asset),
              ZERO)
    received = e.executed_qty - fee
    rec.updates.append(Update(t.intent_id, OPEN, received_qty=received, note="entry filled (found by reconciliation)"))
    return received


def _check_open(t: TrackedTrade, received: Decimal, trader: ReadOnlyTrader, step: Decimal,
                rec: Reconciliation) -> bool:
    """True if the trade is still open and fully protected; records what is wrong otherwise."""
    name = f"{t.symbol} trade {t.intent_id[:8]} ({plain(received)} {t.base_asset})"
    stop = trader.query(t.symbol, t.ids["stop"])
    tp = trader.query(t.symbol, t.ids["take-profit"])
    filled = [o for o in (stop, tp) if o is not None and o.status == "FILLED"]
    if filled:
        other = tp if filled[0] is stop else stop
        if len(filled) == 2:
            rec.mismatches.append(f"{name}: both protective orders filled; more was sold than the trade held")
        elif other is not None and not other.final:
            rec.mismatches.append(f"{name}: closed by one protective order but {other.client_order_id} is still "
                                  f"{other.status}")
        which = "stop-loss" if filled[0] is stop else "take-profit"
        sold = sum((o.executed_qty for o in filled), ZERO)
        _closed(rec, t, received - sold, f"closed on Binance by the {which}")
        return False
    if stop is not None and tp is not None and stop.status == "NEW" and tp.status == "NEW":
        problems = []
        if stop.side != "SELL" or tp.side != "SELL":
            problems.append("a protective order is not a SELL")
        if min(stop.orig_qty, tp.orig_qty) < received - step:
            problems.append(f"protection covers {plain(stop.orig_qty)} / {plain(tp.orig_qty)}")
        if problems:
            rec.unprotected.append(f"{name}: " + "; ".join(problems))
        else:
            rec.checked.append(f"{name}: stop and take-profit open")
        return True
    exit_ = trader.query(t.symbol, t.ids["exit"])
    if exit_ is not None and exit_.status == "FILLED":
        legs = sum((o.executed_qty for o in (stop, tp) if o is not None), ZERO)
        _closed(rec, t, received - legs - exit_.executed_qty, "closed by the bot's market sale")
        return False
    states = " / ".join(o.status if o else "missing" for o in (stop, tp))
    rec.unprotected.append(f"{name}: no confirmed protection (stop / take-profit {states}) and no filled "
                           f"closing sale; sell the coins or restore the protection")
    return True


def _closed(rec: Reconciliation, t: TrackedTrade, left: Decimal, note: str) -> None:
    # A PENDING trade found filled and already closed: the OPEN update is replaced by CLOSED.
    rec.updates[:] = [u for u in rec.updates if u.intent_id != t.intent_id]
    rec.updates.append(Update(t.intent_id, CLOSED, left_qty=max(ZERO, left), note=note))
