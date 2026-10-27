"""The bot's own record of its exchange trades (module 5c): what reconciliation expects to find.

A trade is written here BEFORE its order is sent, as PENDING and keyed by its intent ID (every
client order ID of the trade is derived from it, `bot.core.ids`). So a crash or a timeout never
leaves an order the bot cannot look up. Once the outcome is known the trade becomes OPEN (coins
held, both protective orders confirmed), CLOSED or NOT_OPENED; the last two are final.

The baseline is the coins in the account that are not the bot's (on the testnet, the coins
Binance gives every account). Reconciliation expects, per watched coin: baseline + coins of
OPEN trades + the dust closed trades left behind (less than one step each). The baseline is
set only by an operator with a written reason, and only while no trade is PENDING or OPEN.

Sales (PR-2, audit H2 and M1): every attempt to sell a trade's coins is written here before it is
sent (`note_exit_attempt`), and attempt k has its own client ID (exit leg k), so no ID is ever sent
twice; Binance accepts a repeated client ID once the first order has filled, which could sell coins
twice. At most MAX_EXIT_ATTEMPTS per trade.

The file is replaced atomically. An unreadable file is an error and is never reset: the
caller must block new entries until it is fixed. Version 1 files (before PR-2) still load, with no
sale attempts recorded; files are written as version 2.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.core.ids import client_order_id
from bot.core.money import ZERO, D

LEDGER = "ledger.json"
VERSION = 2
READABLE = (1, 2)                  # version 1: before sale attempts were recorded (PR-2)
MAX_EXIT_ATTEMPTS = 3              # sales tried per trade, exit legs 0 to 2; after that, sell by hand
EXIT_SETTLE_S = 300.0              # a recorded sale not found after this long may count as never placed
PENDING, OPEN, CLOSED, NOT_OPENED = "PENDING", "OPEN", "CLOSED", "NOT_OPENED"
NEXT = {PENDING: (OPEN, CLOSED, NOT_OPENED), OPEN: (CLOSED,), CLOSED: (), NOT_OPENED: ()}


class LedgerError(RuntimeError):
    pass


@dataclass
class TrackedTrade:
    intent_id: str                     # uuid hex
    symbol: str
    base_asset: str
    status: str
    created_at: float
    updated_at: float
    received_qty: Decimal = ZERO       # coins the bot holds while OPEN
    left_qty: Decimal = ZERO           # dust left in the account after the close
    note: str = ""
    strategy: str = ""                 # who proposed it; a strategy manages only its own trades
    expires_at: float = 0.0            # time stop (0: none)
    entry_price: Decimal = ZERO        # average fill, once OPEN
    stop_price: Decimal = ZERO         # the protective stop, once OPEN (the risk engine counts it)
    take_profit_price: Decimal = ZERO
    exit_attempts: int = 0             # sales recorded as sent (attempt k uses exit leg k)
    exit_sent_at: float = 0.0          # when the latest one was recorded

    def cid(self, kind: str, leg: int = 0) -> str:
        return client_order_id(uuid.UUID(hex=self.intent_id), kind, leg)

    @property
    def ids(self) -> dict[str, str]:
        """Every client order ID this trade can have on Binance."""
        out = {"list": self.cid("list"), "entry": self.cid("entry"), "stop": self.cid("protect", 0),
               "take-profit": self.cid("protect", 1), "exit": self.cid("exit")}
        out.update({f"exit-{k}": self.cid("exit", k) for k in range(1, MAX_EXIT_ATTEMPTS)})
        return out

    def exit_ids(self) -> list[str]:
        """The client ID of every sale attempt this trade can have, attempt 0 first."""
        return [self.cid("exit", k) for k in range(MAX_EXIT_ATTEMPTS)]

    def to_json(self) -> dict[str, Any]:
        return {"intent_id": self.intent_id, "symbol": self.symbol, "base_asset": self.base_asset,
                "status": self.status, "created_at": self.created_at, "updated_at": self.updated_at,
                "received_qty": str(self.received_qty), "left_qty": str(self.left_qty), "note": self.note,
                "strategy": self.strategy, "expires_at": self.expires_at, "entry_price": str(self.entry_price),
                "stop_price": str(self.stop_price), "take_profit_price": str(self.take_profit_price),
                "exit_attempts": self.exit_attempts, "exit_sent_at": self.exit_sent_at}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "TrackedTrade":
        if d["status"] not in NEXT:
            raise LedgerError(f"unknown trade status {d['status']!r}")
        return TrackedTrade(d["intent_id"], d["symbol"], d["base_asset"], d["status"], float(d["created_at"]),
                            float(d["updated_at"]), D(d["received_qty"]), D(d["left_qty"]), d.get("note", ""),
                            d.get("strategy", ""), float(d.get("expires_at", 0)), D(d.get("entry_price", "0")),
                            D(d.get("stop_price", "0")), D(d.get("take_profit_price", "0")),
                            int(d.get("exit_attempts", 0)), float(d.get("exit_sent_at", 0)))


@dataclass
class Ledger:
    path: Path
    environment: str
    trades: dict[str, TrackedTrade] = field(default_factory=dict)
    baseline: dict[str, Decimal] = field(default_factory=dict)
    baseline_note: str = ""

    # ------------------------------------------------------------ file
    @staticmethod
    def load(directory: Path, environment: str) -> "Ledger":
        path = Path(directory) / LEDGER
        if not path.exists():
            return Ledger(path, environment)
        try:
            d = json.loads(path.read_text())
            if d.get("version") not in READABLE:
                raise LedgerError(f"ledger version {d.get('version')!r}, expected one of {READABLE}")
            if d.get("environment") != environment:
                raise LedgerError(f"ledger is for {d.get('environment')!r}, not {environment!r}")
            trades = {t["intent_id"]: TrackedTrade.from_json(t) for t in d.get("trades", ())}
            baseline = {a: D(q) for a, q in d.get("baseline", {}).items()}
        except LedgerError as e:
            raise LedgerError(f"{path}: {e}; fix it by hand, it is never reset") from None
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as e:
            raise LedgerError(f"{path} is unreadable ({type(e).__name__}: {e}); fix it by hand, it is never reset") \
                from None
        return Ledger(path, environment, trades, baseline, d.get("baseline_note", ""))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = {"version": VERSION, "environment": self.environment,
                "baseline": {a: str(q) for a, q in sorted(self.baseline.items())}, "baseline_note": self.baseline_note,
                "trades": [t.to_json() for t in self.trades.values()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(body, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ trades
    def active(self) -> list[TrackedTrade]:
        return [t for t in self.trades.values() if t.status in (PENDING, OPEN)]

    def add_pending(self, intent_id: uuid.UUID, symbol: str, base_asset: str, now: float, strategy: str = "",
                    expires_at: float = 0.0) -> TrackedTrade:
        """Call and save BEFORE sending the order."""
        if intent_id.hex in self.trades:
            raise LedgerError(f"intent {intent_id.hex} is already in the ledger; an intent is never sent twice")
        if base_asset not in self.baseline:
            raise LedgerError(f"no baseline for {base_asset}: record one first (bot reconcile --set-baseline)")
        t = TrackedTrade(intent_id.hex, symbol, base_asset, PENDING, now, now, strategy=strategy,
                         expires_at=expires_at)
        self.trades[t.intent_id] = t
        return t

    def mark(self, intent_id: str, status: str, now: float, received_qty: Decimal | None = None,
             left_qty: Decimal | None = None, note: str = "", prices: dict[str, Decimal] | None = None) -> TrackedTrade:
        """`prices`: entry_price, stop_price and take_profit_price, recorded when a trade opens."""
        t = self.trades.get(intent_id)
        if t is None:
            raise LedgerError(f"intent {intent_id} is not in the ledger")
        if status not in NEXT[t.status]:
            raise LedgerError(f"trade {intent_id} cannot go from {t.status} to {status}")
        t.status, t.updated_at = status, now
        if received_qty is not None:
            t.received_qty = D(received_qty)
        if left_qty is not None:
            t.left_qty = max(ZERO, D(left_qty))
        if note:
            t.note = note
        for k, v in (prices or {}).items():
            if k not in ("entry_price", "stop_price", "take_profit_price"):
                raise LedgerError(f"unknown price {k!r}")
            setattr(t, k, D(v))
        return t

    def note_exit_attempt(self, intent_id: str, attempt: int, now: float) -> TrackedTrade:
        """Record that sale `attempt` of a trade is about to be sent. Call and save BEFORE sending it. Attempts only
        go forward (an ID is never reused) and stop at MAX_EXIT_ATTEMPTS."""
        t = self.trades.get(intent_id)
        if t is None:
            raise LedgerError(f"intent {intent_id} is not in the ledger")
        if t.status not in (PENDING, OPEN):
            raise LedgerError(f"trade {intent_id} is {t.status}; nothing of it is sold any more")
        if not t.exit_attempts <= attempt < MAX_EXIT_ATTEMPTS:
            raise LedgerError(f"sale attempt {attempt} of trade {intent_id}: {t.exit_attempts} already recorded, "
                              f"at most {MAX_EXIT_ATTEMPTS}")
        t.exit_attempts, t.exit_sent_at, t.updated_at = attempt + 1, now, now
        return t

    # ------------------------------------------------------------ baseline
    def set_baseline(self, holdings: dict[str, Decimal], operator: str, reason: str, now: float) -> None:
        """Coins in the account that are not the bot's. Resets the dust count of closed trades."""
        if not operator.strip() or len(reason.strip()) < 10:
            raise LedgerError("a baseline needs an operator name and a written reason (10+ characters)")
        if self.active():
            raise LedgerError("cannot set a baseline while a trade is PENDING or OPEN; reconcile it first")
        self.baseline = {a: D(q) for a, q in holdings.items()}
        for t in self.trades.values():
            t.left_qty = ZERO                       # now part of the baseline
        self.baseline_note = f"set by {operator.strip()} at {now:.0f}: {reason.strip()}"

    def expected_holdings(self) -> dict[str, Decimal]:
        """Per watched coin: baseline + coins of OPEN trades + dust left by closed ones."""
        out = dict(self.baseline)
        for t in self.trades.values():
            if t.status == OPEN:
                out[t.base_asset] = out.get(t.base_asset, ZERO) + t.received_qty
            elif t.status == CLOSED:
                out[t.base_asset] = out.get(t.base_asset, ZERO) + t.left_qty
        return out
