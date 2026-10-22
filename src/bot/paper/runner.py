"""Paper-trading runner (module 3): runs the simulator around the clock on live prices.

Every tick (default 5 seconds) it:
1. carries out operator requests (resume, block entries, a manual trade idea),
2. checks the latest prices of held coins and fills any simulated exit,
3. asks each strategy for trade ideas and submits them; `SimExchange.submit`
   runs every one through the risk engine,
4. marks equity (P&L periods, drawdown, breakers) and saves the account.

Operators talk to a running runner through request files in
`<state dir>/requests/`, written by `bot paper resume|block|trade`. Only the
runner changes the account, and a lock file stops a second runner.

Everything that happens is appended to `<state dir>/journal.jsonl`, one JSON
object per line, and printed as one readable line for the service log.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

from bot.core.money import D, fmt
from bot.market.data import MarketDataService, MarketDataUnavailable
from bot.paper import store
from bot.paper.sim import ClosedTrade, EntryResult, SimExchange, as_utc
from bot.portfolio.accounting import MissingPrice
from bot.risk.models import TradeIntent

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
LOCK_FILE = "runner.lock"
REQUESTS_DIR = "requests"


class Strategy(Protocol):
    """A source of trade ideas. Strategies H1-H3 arrive with the backtester (module 4)."""
    name: str

    def propose(self, data: MarketDataService, now: float) -> list[TradeIntent]: ...


class RunnerLocked(RuntimeError):
    pass


# ------------------------------------------------------------ requests (written by the CLI)
def write_request(state_dir: Path, kind: str, **payload: Any) -> Path:
    """Drop a request for the running runner. Written to a temp name, then renamed,
    so the runner never reads half a file."""
    req_dir = Path(state_dir) / REQUESTS_DIR
    req_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}-{kind}"
    tmp = req_dir / f".{name}.tmp"
    tmp.write_text(json.dumps({"type": kind, **payload}))
    final = req_dir / f"{name}.json"
    os.replace(tmp, final)
    return final


def intent_from_request(r: dict[str, Any]) -> TradeIntent:
    return TradeIntent(symbol=str(r["symbol"]), side="BUY", entry_price=D(r["entry"]), stop_price=D(r["stop"]),
                       take_profit_price=D(r["target"]), strategy=str(r.get("strategy") or "manual"),
                       horizon_hours=int(r["hours"]), invalidation=str(r["invalidation"]))


# ------------------------------------------------------------ runner
@dataclass
class PaperRunner:
    sim: SimExchange
    state_dir: Path
    clock: Callable[[], float] = time.time
    strategies: list[Strategy] = field(default_factory=list)
    out: TextIO | None = None                     # readable log lines (stdout for the service)
    status_every_s: float = 900
    _data_down_since: float | None = field(default=None, init=False)
    _last_status: float = field(default=0.0, init=False)
    _seen_history: int = field(default=0, init=False)
    _seen_breaches: frozenset[str] = field(default=frozenset(), init=False)
    stop_requested: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        (self.state_dir / REQUESTS_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
        self._seen_history = len(self.sim.modes.history)
        self._seen_breaches = frozenset(self.sim.modes.active_breaches)

    @classmethod
    def open(cls, state_dir: Path, cfg, market, clock: Callable[[], float] = time.time, **kw) -> "PaperRunner":
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sim, restored = store.load_or_create(state_dir / STATE_FILE, cfg, market, clock)
        runner = cls(sim, state_dir, clock, **kw)
        runner.log("started", restored=restored, equity=_safe_equity(sim), cash=sim.quote,
                   positions=sorted(sim.positions), mode=sim.modes.mode.value,
                   breaches=sorted(sim.modes.active_breaches),
                   note="new entries stay blocked until an operator runs: bot paper resume")
        runner.save()
        return runner

    # ------------------------------------------------------------ journal
    def log(self, event: str, **fields: Any) -> None:
        now = self.clock()
        entry = {"at": now, "event": event, **store.plain(fields)}
        with open(self.state_dir / JOURNAL_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        if self.out is not None:
            detail = " ".join(f"{k}={_readable(v)}" for k, v in fields.items())
            self.out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}Z {event} {detail}\n")
            self.out.flush()

    def save(self) -> None:
        store.save(self.sim, self.state_dir / STATE_FILE, self.clock())

    # ------------------------------------------------------------ one tick
    def tick(self) -> None:
        now = self.clock()
        self._process_requests(now)
        self._check_exits(now)
        for strategy in self.strategies:
            self._run_strategy(strategy, now)
        try:
            self.sim.account_state(now)              # marks equity: day/week rollover, drawdown, breakers
        except MissingPrice as e:
            self.log("mark_failed", reason=str(e))
        self._report_mode_changes()
        if now - self._last_status >= self.status_every_s:
            self._last_status = now
            self.log("status", equity=_safe_equity(self.sim), cash=self.sim.quote,
                     positions=sorted(self.sim.positions), trades=len(self.sim.trades),
                     mode=self.sim.modes.mode.value)
        self.save()

    def run(self, interval_s: float = 5.0, max_ticks: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        ticks = 0
        try:
            while not self.stop_requested and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
                if not self.stop_requested and (max_ticks is None or ticks < max_ticks):
                    sleep(interval_s)
        finally:
            self.save()
            self.log("stopped", ticks=ticks)

    # ------------------------------------------------------------ parts of a tick
    def _check_exits(self, now: float) -> None:
        if not self.sim.positions:
            return
        try:
            trades = self.sim.poll(now)
        except MarketDataUnavailable as e:
            if self._data_down_since is None:
                self._data_down_since = now
                self.log("market_data_down", reason=str(e), positions=sorted(self.sim.positions),
                         note="exits are checked again as soon as prices return; a stop missed meanwhile "
                              "fills at the price seen then")
            return
        if self._data_down_since is not None:
            self.log("market_data_back", down_for_s=round(now - self._data_down_since, 1))
            self._data_down_since = None
        for t in trades:
            self._log_trade(t)

    def _submit(self, intent: TradeIntent, now: float, source: str) -> EntryResult:
        result = self.sim.submit(intent, now)
        d = result.decision
        self.log("entry_filled" if result.filled else ("entry_not_filled" if d.accepted else "entry_rejected"),
                 source=source, symbol=intent.symbol, strategy=intent.strategy,
                 code=d.code.value if d.code else None, reason=result.reason,
                 qty=result.fill.qty if result.fill else d.qty,
                 price=result.fill.price if result.fill else None,
                 fee=result.fill.fee if result.fill else None, planned_risk=d.planned_risk)
        return result

    def _run_strategy(self, strategy: Strategy, now: float) -> None:
        try:
            intents = strategy.propose(self.sim.data, now)
        except Exception as e:   # a broken strategy proposes nothing; exits keep running
            self.log("strategy_error", strategy=getattr(strategy, "name", "?"), reason=f"{type(e).__name__}: {e}")
            return
        for intent in intents:
            self._submit(intent, now, source=f"strategy:{getattr(strategy, 'name', '?')}")

    def _process_requests(self, now: float) -> None:
        req_dir = self.state_dir / REQUESTS_DIR
        for path in sorted(req_dir.glob("*.json")):
            try:
                r = json.loads(path.read_text())
                kind = r.get("type")
                if kind == "resume":
                    try:
                        self.sim.resume(str(r["operator"]), str(r["reason"]), now)
                    except PermissionError as e:
                        self.log("resume_refused", operator=r.get("operator"), reason=str(e))
                elif kind == "block":
                    self.sim.modes.block_entries(str(r["operator"]), str(r["reason"]), as_utc(now))
                elif kind == "trade":
                    self._submit(intent_from_request(r), now, source=f"operator:{r.get('operator', '?')}")
                else:
                    self.log("request_ignored", file=path.name, reason=f"unknown request type {kind!r}")
            except Exception as e:
                self.log("request_failed", file=path.name, reason=f"{type(e).__name__}: {e}")
            finally:
                path.unlink(missing_ok=True)
            self._report_mode_changes()              # keep the journal in the order things happened

    def _report_mode_changes(self) -> None:
        for t in self.sim.modes.history[self._seen_history:]:
            self.log("mode_changed", from_mode=t.from_mode.value, to_mode=t.to_mode.value, actor=t.actor,
                     reason=t.reason)
        self._seen_history = len(self.sim.modes.history)
        now_breaches = frozenset(self.sim.modes.active_breaches)
        for b in sorted(now_breaches - self._seen_breaches):
            self.log("breaker", breach=b)
        self._seen_breaches = now_breaches

    def _log_trade(self, t: ClosedTrade) -> None:
        self.log("trade_closed", **asdict(t))


def _readable(v: Any) -> str:
    """Log-line form: short numbers (the journal keeps full precision)."""
    if isinstance(v, Decimal):
        return fmt(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_readable(x) for x in v) + "]"
    return str(v)


def _safe_equity(sim: SimExchange) -> Decimal | None:
    try:
        return sim.equity()
    except MissingPrice:
        return None


class RunnerLock:
    """Exclusive lock on the state directory, so two runners never share one account."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / LOCK_FILE
        self._f: Any = None

    def __enter__(self) -> "RunnerLock":
        import fcntl  # Linux / macOS; the server runs Ubuntu

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._f = open(self.path, "a+")        # not "w": a failed attempt must not wipe the holder's PID
        try:
            fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._f.close()
            raise RunnerLocked(f"another paper runner is already using {self.path.parent}") from None
        self._f.seek(0)
        self._f.truncate()
        self._f.write(str(os.getpid()))
        self._f.flush()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._f.close()
