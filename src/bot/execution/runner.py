"""Testnet runner (module 5d): H2b places real orders on the Binance SPOT TESTNET (fake money).

Every round (default 60 seconds) it:
1. reconciles the ledger with the testnet account (`bot.execution.reconcile`) and records what is
   certain. Coins found unprotected are sold at market at once. A mismatch or unprotected coins
   block new entries until an operator resumes; an exchange error or an order still unsettled
   blocks entries for that round only;
2. marks the bot's equity (`bot.execution.account`: capped capital, P&L periods, drawdown,
   breakers) and carries out operator requests (resume, block entries);
3. sells trades whose time stop has passed;
4. once per finished candle of each strategy's interval: the strategy's exits for its own open
   trades, then its new ideas. Each idea is written to the ledger as PENDING, then goes to
   `bot.execution.entry.submit()`, which runs the risk engine and sends one protected order list.
   The candle each strategy decided on is saved first (`strategy-clock.json`), so a restart
   neither repeats nor skips a decision.

Signals come from live market data (the same prices the forward test sees); orders, the risk
engine's market snapshot and valuations use the testnet's own prices and rules. Testnet only:
`BinanceTrader` refuses any other environment. Everything that happens goes to `journal.jsonl`
in the state directory and to the service log.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

from bot.core.config import AppConfig
from bot.core.ids import new_intent_id
from bot.core.money import ZERO, fmt
from bot.exchange.binance import ExchangeError
from bot.exchange.interface import INTERVAL_MS
from bot.exchange.trading import BinanceTrader, OrderStatusUnknown
from bot.execution import entry, exit as exits, reconcile
from bot.execution.account import QUOTE, TradingAccount, as_utc
from bot.execution.ledger import CLOSED, NOT_OPENED, OPEN, Ledger, LedgerError, TrackedTrade
from bot.market.data import MarketDataService, MarketDataUnavailable
from bot.portfolio.accounting import MissingPrice
from bot.risk.models import AccountState, Mode, TradeIntent
from bot.strategies.base import Action, Exit, OpenTrade

JOURNAL_FILE = "journal.jsonl"
REQUESTS_DIR = "requests"
CLOCK_FILE = "strategy-clock.json"
RETRY_S = 60
STATUS_EVERY_S = 900


class Strategy(Protocol):
    name: str
    interval: str

    def propose(self, data: MarketDataService, now: float) -> list[TradeIntent]: ...

    def manage(self, data: MarketDataService, now: float, open_trades: list[OpenTrade]) -> list[Action]: ...


class RunnerError(RuntimeError):
    pass


@dataclass
class TestnetRunner:
    __test__ = False                     # not a pytest test class
    state_dir: Path
    cfg: AppConfig
    trader: BinanceTrader
    exchange: MarketDataService          # the testnet: rules, books, the risk engine's snapshot
    signals: MarketDataService           # live market data the strategies decide on
    strategies: list[Strategy]
    corr_groups: dict[str, str]
    ledger: Ledger
    account: TradingAccount
    clock: Callable[[], float] = time.time
    out: TextIO | None = None
    stop_requested: bool = field(default=False, init=False)
    _decided: dict[str, int] = field(default_factory=dict, init=False)
    _failed: dict[str, tuple[int, float]] = field(default_factory=dict, init=False)
    _blocked_reason: str = field(default="", init=False)
    _last_status: float = field(default=0.0, init=False)
    _seen_history: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.trader.environment != "testnet":
            raise RunnerError("the runner trades on the testnet only")
        self.state_dir = Path(self.state_dir)
        (self.state_dir / REQUESTS_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
        self._decided = _load_clock(self.state_dir / CLOCK_FILE)
        self._seen_history = len(self.account.modes.history)

    # ------------------------------------------------------------ journal
    def log(self, event: str, **fields: Any) -> None:
        now = self.clock()
        row = {"at": now, "event": event, **_plain(fields)}
        with open(self.state_dir / JOURNAL_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
        if self.out is not None:
            detail = " ".join(f"{k}={_readable(v)}" for k, v in fields.items())
            self.out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}Z {event} {detail}\n")
            self.out.flush()

    # ------------------------------------------------------------ the loop
    def run(self, interval_s: float = 60.0, max_ticks: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        self.log("started", capital=self.account.capital, mode=self.account.modes.mode.value,
                 breaches=sorted(self.account.modes.active_breaches), open=len(self.ledger.active()),
                 note="new entries stay blocked until an operator runs: bot testnet resume")
        ticks = 0
        try:
            while not self.stop_requested and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
                if not self.stop_requested and (max_ticks is None or ticks < max_ticks):
                    sleep(interval_s)
        finally:
            self.account.save()
            self.log("stopped", ticks=ticks)

    def tick(self) -> None:
        now = self.clock()
        rec = self._reconcile(now)
        clean = rec.clean
        state = self._state(now, entries_ok=clean)
        self._requests(now, state)
        self._time_stops(now)
        for s in self.strategies:
            self._run_strategy(s, now, entries_ok=clean)
        self._report_mode_changes()
        if state is not None and now - self._last_status >= STATUS_EVERY_S:
            self._last_status = now
            self.log("status", equity=state.equity, cash=state.free_quote, drawdown_pct=state.drawdown_pct,
                     open=[t.symbol for t in self.ledger.trades.values() if t.status == OPEN],
                     mode=state.mode.value)
        self.account.save()

    # ------------------------------------------------------------ 1. reconciliation
    def _tolerances(self) -> dict[str, Decimal]:
        out = {}
        for asset in set(self.ledger.baseline) | {t.base_asset for t in self.ledger.trades.values()}:
            r = self.exchange.rules(asset + QUOTE)
            out[asset] = max(r.step_size, r.market_step_size or ZERO)
        return out

    def _reconcile(self, now: float) -> reconcile.Reconciliation:
        try:
            tol = self._tolerances()
        except MarketDataUnavailable as e:
            rec = reconcile.Reconciliation(errors=[f"exchange rules: {e}"])
        else:
            rec = reconcile.reconcile(self.ledger, self.trader, tol, now)
            reconcile.apply(self.ledger, rec, now)
        for u in rec.updates:
            self.log("ledger_update", trade=u.intent_id[:8], status=u.status, note=u.note,
                     symbol=self.ledger.trades[u.intent_id].symbol)
        for item in rec.mismatches:
            self.log("reconciliation_mismatch", detail=item)
        for item in rec.unprotected:
            self.log("unprotected", detail=item, note="selling at market now; entries blocked")
        if rec.clean:                       # fixed: the faults go, but entries stay blocked until an operator resumes
            for fault in ("RECONCILIATION_MISMATCH", "UNPROTECTED_POSITION"):
                if fault in self.account.modes.active_breaches:
                    self.account.modes.clear_fault(fault)
                    self.log("fault_cleared", fault=fault, note="reconciliation is clean again; entries stay "
                                                                "blocked until: bot testnet resume")
        if rec.mismatches:
            self.account.modes.report_fault("RECONCILIATION_MISMATCH", rec.mismatches[0])
        if rec.unprotected:
            self.account.modes.report_fault("UNPROTECTED_POSITION", rec.unprotected[0])
            for iid in rec.unprotected_ids:
                self._close(self.ledger.trades[iid], now, "unprotected coins sold at market")
        reason = "; ".join(rec.errors + rec.unsettled)
        if reason != self._blocked_reason:
            if reason:
                self.log("entries_paused", reason=reason, note="for as long as this lasts; no operator needed")
            else:
                self.log("entries_unpaused")
            self._blocked_reason = reason
        return rec

    # ------------------------------------------------------------ 2. equity, requests
    def _bids(self) -> dict[str, Decimal]:
        symbols = sorted({a + QUOTE for a in self.account.start if a != QUOTE} |
                         {t.symbol for t in self.ledger.trades.values() if t.status == OPEN})
        return {s: b.bid for s, b in self.exchange.books(symbols).items()}

    def _state(self, now: float, entries_ok: bool) -> AccountState | None:
        """Mark the bot's equity and return the risk engine's view; None if it cannot be valued now."""
        try:
            info = self.trader.account()
            totals = {b.asset: b.total for b in info.balances}
            free = next((b.free for b in info.balances if b.asset == QUOTE), ZERO)
            return self.account.state(self.ledger, totals, free, self._bids(), self.corr_groups, now, entries_ok)
        except (ExchangeError, MarketDataUnavailable, MissingPrice) as e:
            self.log("mark_failed", reason=str(e), note="no new entries until the account can be valued")
            return None

    def _requests(self, now: float, state: AccountState | None) -> None:
        for path in sorted((self.state_dir / REQUESTS_DIR).glob("*.json")):
            try:
                r = json.loads(path.read_text())
                kind = r.get("type")
                if kind == "resume":
                    if state is None:
                        self.log("resume_refused", operator=r.get("operator"),
                                 reason="the account cannot be valued right now; try again")
                    else:
                        try:
                            self.account.resume(str(r["operator"]), str(r["reason"]), state, now)
                        except PermissionError as e:
                            self.log("resume_refused", operator=r.get("operator"), reason=str(e))
                elif kind == "block":
                    self.account.modes.block_entries(str(r["operator"]), str(r["reason"]), as_utc(now))
                else:
                    self.log("request_ignored", file=path.name, reason=f"{kind!r} is not a testnet runner request")
            except Exception as e:
                self.log("request_failed", file=path.name, reason=f"{type(e).__name__}: {e}")
            finally:
                path.unlink(missing_ok=True)
            self._report_mode_changes()

    def _report_mode_changes(self) -> None:
        for t in self.account.modes.history[self._seen_history:]:
            self.log("mode_changed", from_mode=t.from_mode.value, to_mode=t.to_mode.value, actor=t.actor,
                     reason=t.reason)
        self._seen_history = len(self.account.modes.history)

    # ------------------------------------------------------------ 3-4. exits and strategies
    def _time_stops(self, now: float) -> None:
        for t in list(self.ledger.trades.values()):
            if t.status == OPEN and t.expires_at and now >= t.expires_at:
                self._close(t, now, "time stop")

    def _close(self, t: TrackedTrade, now: float, why: str) -> None:
        try:
            rules = self.exchange.rules(t.symbol)
        except MarketDataUnavailable as e:
            self.log("exit_failed", symbol=t.symbol, trade=t.intent_id[:8], why=why, reason=str(e))
            return
        r = exits.close_trade(t, self.trader, rules)
        if not r.closed:
            self.log("exit_failed", symbol=t.symbol, trade=t.intent_id[:8], why=why, reason=r.problem,
                     note="ACT NOW if this repeats: the coins may be unprotected; check bot reconcile")
            self.account.modes.report_fault("UNPROTECTED_POSITION", f"{t.symbol}: {r.problem}")
            return
        self.ledger.mark(t.intent_id, CLOSED, now, left_qty=r.left_qty, note=why)
        self.ledger.save()
        self.log("trade_closed", symbol=t.symbol, trade=t.intent_id[:8], why=why, strategy=t.strategy,
                 qty=t.received_qty, entry=t.entry_price,
                 sold_at=(r.sale.quote_qty / r.sale.executed_qty) if r.sale and r.sale.executed_qty else None)

    def _run_strategy(self, strategy: Strategy, now: float, entries_ok: bool) -> None:
        name = getattr(strategy, "name", "?")
        step = INTERVAL_MS[strategy.interval]
        candle = int(now * 1000) // step * step
        last = self._decided.get(name)
        if last is not None and last >= candle:
            return
        failed = self._failed.get(name)
        if failed is not None and failed[0] == candle and now - failed[1] < RETRY_S:
            return
        mine = [t for t in self.ledger.trades.values() if t.status == OPEN and t.strategy == name]
        try:
            actions = strategy.manage(self.signals, now, [OpenTrade(t.symbol, t.entry_price, t.stop_price,
                                                                    t.created_at) for t in mine]) if mine else []
            intents = strategy.propose(self.signals, now)
        except Exception as e:
            if failed is None or failed[0] != candle:
                self.log("strategy_error", strategy=name, reason=f"{type(e).__name__}: {e}",
                         note=f"retried every {RETRY_S} s until this candle's decision succeeds")
            self._failed[name] = (candle, now)
            return
        self._failed.pop(name, None)
        if last is not None and candle - last > step:
            self.log("decisions_missed", strategy=name, candles=(candle - last) // step - 1)
        self._decided[name] = candle
        _save_clock(self.state_dir / CLOCK_FILE, self._decided)    # before acting: never decide twice
        for a in actions:
            if isinstance(a, Exit):
                for t in mine:
                    if t.symbol == a.symbol and t.status == OPEN:
                        self._close(t, now, f"strategy exit: {a.reason}")
            # A raised stop would mean replacing the protection on the exchange; H2b never raises one.
        for intent in intents:
            self._enter(intent, now, entries_ok)

    def _enter(self, intent: TradeIntent, now: float, entries_ok: bool) -> None:
        state = self._state(now, entries_ok)
        if state is not None and state.mode is not Mode.RUNNING:
            self.log("entry_blocked", symbol=intent.symbol, strategy=intent.strategy, mode=state.mode.value,
                     breaches=sorted(self.account.modes.active_breaches), reason=self._blocked_reason or None)
            return
        try:
            if state is None:
                raise MarketDataUnavailable("the account cannot be valued")
            rules = self.exchange.rules(intent.symbol)
            snap = self.exchange.snapshot(intent.symbol)
        except MarketDataUnavailable as e:
            self.log("entry_skipped", symbol=intent.symbol, strategy=intent.strategy, reason=str(e))
            return
        iid = new_intent_id()
        try:
            self.ledger.add_pending(iid, intent.symbol, rules.base_asset, now, intent.strategy,
                                    expires_at=now + intent.horizon_hours * 3600)
        except LedgerError as e:
            self.log("entry_skipped", symbol=intent.symbol, strategy=intent.strategy, reason=str(e))
            return
        self.ledger.save()                                  # written before anything is sent
        try:
            out = entry.submit(intent, state, snap, rules, self.cfg, self.trader, iid,
                               self.corr_groups.get(intent.symbol, "majors"))
        except (OrderStatusUnknown, ExchangeError) as e:
            self.log("entry_unknown", symbol=intent.symbol, trade=iid.hex[:8], reason=str(e),
                     note="the trade stays PENDING; reconciliation settles it and blocks entries meanwhile")
            return
        record_entry(self.ledger, out, iid.hex, rules.base_asset, now)
        d = out.decision
        self.log("entry_" + out.status.lower(), symbol=intent.symbol, strategy=intent.strategy, trade=iid.hex[:8],
                 code=d.code.value if d is not None and d.code else None, reason=out.reason,
                 qty=out.received_qty, price=entry_price(out), planned_risk=d.planned_risk if d else None)
        if out.block_entries:
            self.account.modes.block_entries("system", f"{intent.symbol} entry {out.status}: {out.reason}")
            if out.status == entry.UNPROTECTED:
                self.account.modes.report_fault("UNPROTECTED_POSITION", out.reason)


# ---------------------------------------------------------------- the ledger after an entry
def entry_price(out: entry.EntryOutcome) -> Decimal | None:
    e = out.entry
    return e.quote_qty / e.executed_qty if e is not None and e.executed_qty > 0 else None


def record_entry(ledger: Ledger, out: entry.EntryOutcome, intent_id: str, base_asset: str, now: float) -> None:
    """PENDING becomes OPEN (coins held), CLOSED (sold again at once) or NOT_OPENED; saved."""
    held = out.received_qty or (entry.received_qty(out.entry, base_asset) if out.entry else ZERO)
    if out.status in (entry.PROTECTED, entry.UNPROTECTED):
        p = out.plan
        ledger.mark(intent_id, OPEN, now, received_qty=held, note=out.status,
                    prices={"entry_price": entry_price(out) or p.entry_price, "stop_price": p.stop_price,
                            "take_profit_price": p.take_profit_price})
    elif out.status == entry.EMERGENCY_EXIT:
        ledger.mark(intent_id, CLOSED, now, received_qty=held, left_qty=held - out.exit.executed_qty,
                    note="emergency exit")
    else:
        ledger.mark(intent_id, NOT_OPENED, now, note=f"{out.status}: {out.reason}"[:200])
    ledger.save()


# ---------------------------------------------------------------- small helpers
def _load_clock(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
    except Exception as e:
        raise RunnerError(f"{path} is unreadable ({type(e).__name__}: {e}); fix it by hand after checking "
                          f"the journal") from None


def _save_clock(path: Path, decided: dict[str, int]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(decided))
    os.replace(tmp, path)


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, uuid.UUID):
        return v.hex
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _readable(v: Any) -> str:
    if isinstance(v, Decimal):
        return fmt(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_readable(x) for x in v) + "]"
    return str(v)
