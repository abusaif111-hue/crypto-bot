"""Reconciliation (module 5c): does Binance hold what the bot's ledger says, and nothing else?

Read-only: it looks orders up and reads balances; it never places or cancels anything (enforced
by tests). What it finds goes to the caller, which blocks new entries unless the result is clean
(`faults()` names the mode-controller faults to report).

Checked, every run:
- PENDING trades (sent, outcome not recorded): the entry is looked up by its client ID. Not found and
  younger than SETTLE_S seconds, it stays unsettled. Not found after that is NOT proof on its own: it
  counts as never placed only if Binance also has none of the trade's protective or closing orders, no
  fill on the coin since the trade was written that cannot be shown to be someone else's, and no more of
  the coin than the ledger expects. Each such fill's order is looked up by its order ID (PR-1b):
  - it is this trade's entry (its client ID and coin, a BUY, finished, something bought): the trade is
    settled from that order, as if it had been found by its client ID;
  - it is another of the bot's trades in this ledger: not evidence;
  - it is not the bot's (an ID the bot never makes): not evidence, and the coins it bought or sold are
    allowed for in the coin comparison;
  - anything else (the order cannot be found or looked up, the reply does not match the fill, an ID in
    the bot's shape that this ledger does not know, too many orders): evidence.
  Any evidence means the outcome is uncertain: a mismatch, the trade stays PENDING, and new entries stay
  blocked (the runner also refuses every entry while any trade is PENDING). A filled entry is checked
  like an OPEN trade, with the coins received worked out from its fills.
- OPEN trades: both protective orders must be open SELLs covering the coins (less one step). One
  filled means Binance closed the trade (the other must have ended); both gone means the bot's
  own market sale closed it, or, if that sale did not fill, the coins are UNPROTECTED.
- Open orders: every open order on the account must belong to a PENDING or OPEN trade.
- Coins: for each coin in the baseline, Binance's total (free + locked) must equal the ledger's
  expected holding within the coin's tolerance (one step). Skipped while a trade is unsettled.
  USDT is not compared: it moves with prices, fees and P&L.

Updates it is certain of (never placed, opened, closed) are returned for `apply()`; an exchange
error discards them all and fails closed. A reply Binance sent but that cannot be read
(`MalformedResponse`) is such an error too, and is also listed in `unreadable` so the operator is told.
Fees paid in BNB move the BNB balance: with BNB in the
baseline that shows as a mismatch (fail closed). Verify on testnet: how soon a timed-out order
appears in GET /api/v3/order (SETTLE_S).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol

from bot.core.ids import is_bot_client_id
from bot.core.money import ZERO, plain
from bot.exchange.binance import ExchangeError
from bot.exchange.trading import TRADES_PAGE, AccountInfo, MalformedResponse, OrderState, TradeFill, TradeRecord
from bot.execution.ledger import EXIT_SETTLE_S, CLOSED, NOT_OPENED, OPEN, PENDING, Ledger, TrackedTrade

SETTLE_S = 300.0
ENDED = ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")
FILL_MARGIN_MS = 60_000                      # look for fills from a minute before the trade was written (clocks)
FILL_WINDOW_MS = 24 * 3_600_000 - 1          # Binance allows at most 24 hours per myTrades request (verify on testnet)
ATTRIBUTE_MAX = 20                           # most orders looked up behind one pending trade's fills (weight 4 each)


class ReadOnlyTrader(Protocol):
    """The only trader methods reconciliation may use."""

    def query(self, symbol: str, client_order_id: str) -> OrderState | None: ...
    def query_by_id(self, symbol: str, order_id: int) -> OrderState | None: ...
    def open_orders(self, symbol: str | None = None) -> list[OrderState]: ...
    def account(self) -> AccountInfo: ...
    def my_trades(self, symbol: str, order_id: int) -> list[TradeFill]: ...
    def trades_between(self, symbol: str, start_ms: int, end_ms: int) -> list[TradeRecord]: ...


@dataclass(frozen=True)
class Update:
    intent_id: str
    status: str
    received_qty: Decimal | None = None
    left_qty: Decimal | None = None
    note: str = ""
    prices: dict[str, Decimal] | None = None   # entry, stop and take-profit of a trade found open


@dataclass
class Reconciliation:
    mismatches: list[str] = field(default_factory=list)     # something differs: an operator must look
    unprotected: list[str] = field(default_factory=list)    # coins held without confirmed protection: act now
    unsettled: list[str] = field(default_factory=list)      # an order's outcome is not known yet: wait
    errors: list[str] = field(default_factory=list)         # Binance could not be read: try again
    unreadable: list[str] = field(default_factory=list)     # of those, a reply that could not be read: alert
    updates: list[Update] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)        # what matched, for the report
    unprotected_ids: list[str] = field(default_factory=list)    # intent IDs of the trades in `unprotected`

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
    except MalformedResponse as e:
        return Reconciliation(errors=[f"could not read Binance: {e}"], unreadable=[str(e)])
    except ExchangeError as e:
        return Reconciliation(errors=[f"could not read Binance: {e}"])
    return rec


def apply(ledger: Ledger, rec: Reconciliation, now: float) -> None:
    """Record the certain updates and save. Only for a run without exchange errors."""
    if rec.errors:
        return
    for u in rec.updates:
        ledger.mark(u.intent_id, u.status, now, received_qty=u.received_qty, left_qty=u.left_qty, note=u.note,
                    prices=u.prices)
    if rec.updates:
        ledger.save()


# ---------------------------------------------------------------- checks
def _check(ledger: Ledger, trader: ReadOnlyTrader, tolerance: dict[str, Decimal], now: float,
           rec: Reconciliation) -> None:
    held: dict[str, Decimal] = {}                     # coins of trades that are OPEN after this run
    for t in list(ledger.active()):
        if t.status == PENDING:
            received = _resolve_pending(t, trader, now, rec, ledger, tolerance)
        else:
            received = t.received_qty
        if received is not None:
            if _check_open(t, received, trader, tolerance.get(t.base_asset, ZERO), rec, now):
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


def _resolve_pending(t: TrackedTrade, trader: ReadOnlyTrader, now: float, rec: Reconciliation, ledger: Ledger,
                     tolerance: dict[str, Decimal]) -> Decimal | None:
    """The coins received if the entry filled; None if the trade did not open (or is not settled)."""
    e = trader.query(t.symbol, t.ids["entry"])
    if e is None:
        age = now - t.created_at
        if age < SETTLE_S:
            rec.unsettled.append(f"{t.symbol} entry {t.ids['entry']}: not found yet; waiting "
                                 f"{SETTLE_S:.0f} s, then checking every other record before deciding")
            return None
        ex = examine(t, trader, now, ledger, tolerance)
        if ex.entry is not None:                    # found from its own fill: settled from that order below
            e, how = ex.entry, (f"entry filled (found from its fill, order {ex.entry.order_id}; Binance did not "
                                f"find it by its client ID)")
        elif ex.evidence:
            # No age or time in this text: it is the alert's key, so it must stay the same from round to round
            # while nothing changes, or every round would send a new alert.
            rec.mismatches.append(f"{t.symbol} entry {t.ids['entry']}: not found {SETTLE_S:.0f} s or more after "
                                  f"it was written, but {'; '.join(ex.evidence)}. The outcome is uncertain: the "
                                  f"trade stays PENDING and new entries stay blocked until it is settled")
            return None
        else:
            rec.updates.append(Update(t.intent_id, NOT_OPENED,
                                      note=f"not found after {age:.0f} s, and Binance has no protective or closing "
                                           f"order of the trade, no fill on the coin since it was written that is "
                                           f"not someone else's, and no extra coins: never placed"))
            return None
    else:
        how = "entry filled (found by reconciliation)"
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
    rec.updates.append(Update(t.intent_id, OPEN, received_qty=received, note=how,
                              prices={"entry_price": e.quote_qty / e.executed_qty}))
    return received


@dataclass(frozen=True)
class FillOrder:
    """One Binance order behind fills on the coin since a pending trade was written, and whose it is."""
    order_id: int
    fills: int
    bought: Decimal                   # coins bought in these fills (less any fee taken in the coin)
    sold: Decimal                     # coins sold (plus any fee taken in the coin)
    client_order_id: str = ""         # empty: the order could not be found
    status: str = ""
    owner: str = ""                   # whose order it is, in words
    evidence: str = ""                # not empty: why it keeps the trade uncertain
    order: OrderState | None = field(default=None, repr=False, compare=False)


@dataclass
class Examination:
    """Everything reconciliation looks at for a pending entry it cannot find by its client ID (read-only)."""
    start_ms: int
    end_ms: int
    own_orders: list[str] = field(default_factory=list)      # the trade's protective or closing orders found
    fills: int = 0
    orders: list[FillOrder] = field(default_factory=list)
    others_net: Decimal = ZERO        # coins that orders not the bot's added (bought less sold) in the window
    extra: Decimal = ZERO             # coins beyond the ledger, after allowing for others_net
    entry: OrderState | None = None   # this trade's entry, found from its fill
    evidence: list[str] = field(default_factory=list)


def examine(t: TrackedTrade, trader: ReadOnlyTrader, now: float, ledger: Ledger,
            tolerance: dict[str, Decimal]) -> Examination:
    """Signs on Binance that a PENDING trade's entry was executed although its order cannot be found by its
    client ID, or the entry itself, found from its fill. Reads only."""
    start = int(t.created_at * 1000) - FILL_MARGIN_MS
    ex = Examination(start, min(int(now * 1000), start + FILL_WINDOW_MS))
    for kind in ("stop", "take-profit", "exit"):
        o = trader.query(t.symbol, t.ids[kind])
        if o is not None:
            ex.own_orders.append(f"its {kind} order {o.client_order_id} exists ({o.status})")
    ex.evidence += ex.own_orders
    fills = trader.trades_between(t.symbol, ex.start_ms, ex.end_ms)
    ex.fills = len(fills)
    by_order: dict[int, list[TradeRecord]] = {}
    for f in fills:
        by_order.setdefault(f.order_id, []).append(f)
    if len(fills) >= TRADES_PAGE:
        ex.evidence.append(f"{len(fills)} fills on {t.symbol} since the trade was written, too many to check")
    elif len(by_order) > ATTRIBUTE_MAX:
        ex.evidence.append(f"fills from {len(by_order)} orders on {t.symbol} since the trade was written, too many "
                           f"to identify")
    else:
        known = {cid: other for other in ledger.trades.values() if other.intent_id != t.intent_id
                 for cid in other.ids.values()}
        for oid, fs in sorted(by_order.items()):
            ex.orders.append(_identify(t, trader, oid, fs, known))
        entries = [o for o in ex.orders if o.owner == "this trade's entry"]
        if len(entries) > 1:
            ex.evidence.append(f"{len(entries)} orders carry this trade's entry ID")
        ex.evidence += [o.evidence for o in ex.orders if o.evidence]
        ex.others_net = sum((o.bought - o.sold for o in ex.orders if o.owner == "not the bot's"), ZERO)
        if len(entries) == 1:
            ex.entry = entries[0].order
    have = next((b.total for b in trader.account().balances if b.asset == t.base_asset), ZERO)
    want = ledger.expected_holdings().get(t.base_asset, ZERO)
    ex.extra = have - want - ex.others_net
    if ex.extra > tolerance.get(t.base_asset, ZERO):
        after = f" after allowing for orders that are not the bot's ({plain(ex.others_net)})" if ex.others_net else ""
        ex.evidence.append(f"Binance holds {plain(ex.extra)} more {t.base_asset} than the ledger expects{after}")
    return ex


def _is_entry(t: TrackedTrade, o: OrderState) -> bool:
    """This trade's own entry, and conclusive: its client ID, the coin, a BUY, finished, something bought."""
    return (o.client_order_id == t.ids["entry"] and o.symbol == t.symbol and o.side == "BUY" and o.final
            and o.executed_qty > 0)


def _identify(t: TrackedTrade, trader: ReadOnlyTrader, oid: int, fs: list[TradeRecord],
              known: dict[str, TrackedTrade]) -> FillOrder:
    """Whose order `oid` is. A lookup that fails is evidence (uncertain), never 'not the bot's'."""
    fee = [f.commission if f.commission_asset == t.base_asset else ZERO for f in fs]
    bought = sum((f.qty - c for f, c in zip(fs, fee) if f.is_buyer), ZERO)
    sold = sum((f.qty + c for f, c in zip(fs, fee) if not f.is_buyer), ZERO)
    base = {"order_id": oid, "fills": len(fs), "bought": bought, "sold": sold}
    what = f"order {oid} ({len(fs)} fill(s) on {t.symbol} since the trade was written)"
    try:
        o = trader.query_by_id(t.symbol, oid)
    except ExchangeError as e:                      # incl. an unreadable reply: unknown, so uncertain
        return FillOrder(**base, owner="unknown", evidence=f"{what} could not be looked up ({type(e).__name__})")
    if o is None:
        return FillOrder(**base, owner="unknown", evidence=f"{what}: Binance cannot find that order")
    found = {**base, "client_order_id": o.client_order_id, "status": o.status, "order": o}
    if o.order_id != oid or o.symbol != t.symbol:
        return FillOrder(**found, owner="unknown",
                         evidence=f"{what}: the reply is for {o.symbol} order {o.order_id}, not this one")
    if {f.is_buyer for f in fs} != {o.side == "BUY"}:
        return FillOrder(**found, owner="unknown", evidence=f"{what}: its fills do not match its side ({o.side})")
    cid = o.client_order_id
    if cid == t.ids["entry"]:
        if _is_entry(t, o):
            return FillOrder(**found, owner="this trade's entry")
        return FillOrder(**found, owner="unknown", evidence=f"{what} carries this trade's entry ID but is "
                                                            f"{o.side} {o.status} with {plain(o.executed_qty)} bought")
    own = next((kind for kind, c in t.ids.items() if c == cid), None)
    if own is not None:
        return FillOrder(**found, owner=f"this trade's {own} order", evidence=f"{what} is this trade's {own} order")
    other = known.get(cid)
    if other is not None:
        return FillOrder(**found, owner=f"the bot's trade {other.intent_id[:8]} ({other.status})")
    if is_bot_client_id(cid):
        return FillOrder(**found, owner="unknown", evidence=f"{what} has a bot-made ID ({cid}) this ledger does "
                                                            f"not know")
    return FillOrder(**found, owner="not the bot's")


def _check_open(t: TrackedTrade, received: Decimal, trader: ReadOnlyTrader, step: Decimal,
                rec: Reconciliation, now: float) -> bool:
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
            rec.unprotected_ids.append(t.intent_id)
        else:
            rec.checked.append(f"{name}: stop and take-profit open")
        for i, u in enumerate(rec.updates):          # found open this run: record its protective prices
            if u.intent_id == t.intent_id and u.status == OPEN:
                rec.updates[i] = replace(u, prices={**(u.prices or {}), "stop_price": stop.stop_price or ZERO,
                                                    "take_profit_price": tp.price})
        return True
    # The bot's own sales (PR-2): every attempt has its own ID; what they sold together counts.
    sales = [trader.query(t.symbol, cid) for cid in t.exit_ids()]
    found = [k for k, o in enumerate(sales) if o is not None]
    legs = sum((o.executed_qty for o in (stop, tp) if o is not None), ZERO)
    sold = sum((o.executed_qty for o in sales if o is not None), ZERO)
    still = next((o for o in sales if o is not None and not o.final), None)
    if still is not None:
        rec.unsettled.append(f"{name}: its closing sale {still.client_order_id} is still {still.status}")
        return True
    if legs + sold > received + step:
        rec.mismatches.append(f"{name}: its sales and protective orders sold {plain(legs + sold)}, more than the "
                              f"{plain(received)} it held")
    made = max(t.exit_attempts, found[-1] + 1 if found else 0)
    if found and found[-1] == made - 1 and sales[found[-1]].status == "FILLED":
        _closed(rec, t, received - legs - sold, "closed by the bot's market sale")
        return False
    if made and sales[made - 1] is None and now - t.exit_sent_at < EXIT_SETTLE_S:
        rec.unsettled.append(f"{name}: its closing sale {t.exit_ids()[made - 1]} cannot be found yet; waiting "
                             f"{EXIT_SETTLE_S:.0f} s before deciding")
        return True
    states = " / ".join(o.status if o else "missing" for o in (stop, tp))
    rec.unprotected.append(f"{name}: no confirmed protection (stop / take-profit {states}) and no filled "
                           f"closing sale; sell the coins or restore the protection")
    rec.unprotected_ids.append(t.intent_id)
    return True


def _closed(rec: Reconciliation, t: TrackedTrade, left: Decimal, note: str) -> None:
    # A PENDING trade found filled and already closed: the OPEN update is replaced by CLOSED.
    rec.updates[:] = [u for u in rec.updates if u.intent_id != t.intent_id]
    rec.updates.append(Update(t.intent_id, CLOSED, left_qty=max(ZERO, left), note=note))
