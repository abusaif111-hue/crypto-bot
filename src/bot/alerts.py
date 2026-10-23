"""Telegram alerts (module 5e): the bot tells Sultan's phone what happened and what needs him.

A separate service (`bot alerts run`, service `bot-alerts`) reads the journals the runners already
write (testnet runner, paper account, H2b forward test) and sends a short Telegram message for what
matters. It only reads files and sends messages: it imports nothing that trades, so a broken alert
can never change an order, and the runners never wait for Telegram.

Three levels, at the start of each line: 🔴 act now (coins unprotected, a mismatch, a failed exit,
an order whose outcome is unknown, a breaker), 🟡 worth a look (entries blocked, a runner restarted or
silent, errors that repeat), 🟢 for the record (a trade opened or closed, entries resumed, a fault
cleared). Short outages are not reported: entries paused or market data down for less than
PAUSE_GRACE_S are swallowed when they recover. Repeats of the same alert are held back for
REPEAT_S (🔴) or THROTTLE_S (🟡). A runner that writes nothing for SILENT_S is reported as silent.
Once a day (HEARTBEAT_HOUR_UTC, 08:00 Muscat) a short check says each runner's mode and equity, so
silence from the alert service itself is noticed too.

The bot token is a secret: it is never printed, logged or put in an error (`Telegram` scrubs it).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

RED, YELLOW, GREEN = "🔴", "🟡", "🟢"
PAUSE_GRACE_S = 600
THROTTLE_S = 3600
REPEAT_S = 900
SILENT_S = 2400
HEARTBEAT_HOUR_UTC = 4
MAX_MESSAGE = 3900                     # Telegram's limit is 4096 characters
MAX_QUEUE = 200
STATE_FILE = "alerts-state.json"
API = "https://api.telegram.org"

# Journals the alert service reads: name shown in messages -> folder (under the bot user's home).
SOURCES = {"testnet": "testnet", "paper": "paper", "forward": "forward-h2b"}
JOURNAL = "journal.jsonl"
_DELAYED = {"entries_paused": "entries_unpaused", "market_data_down": "market_data_back"}


@dataclass(frozen=True)
class Alert:
    level: str
    source: str
    text: str
    key: str = ""                      # what counts as "the same alert" for throttling

    def line(self) -> str:
        return f"{self.level} {self.source}: {' '.join(self.text.split())}"     # one line, always


# ---------------------------------------------------------------- what each event means
def _n(v: Any, places: int = 2) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    if abs(d) >= 1 or d == 0:
        return f"{d:,.{places}f}"
    return f"{d.normalize():f}"


def describe(source: str, e: dict[str, Any]) -> Alert | None:
    """The alert for one journal event, or None if it needs none. Pure."""
    ev, sym = e.get("event", ""), e.get("symbol", "")

    def a(level: str, text: str, key: str = "") -> Alert:
        return Alert(level, source, text, key or ev)

    if ev in ("entry_protected", "entry_filled"):
        risk = f"; worst planned loss {_n(e['planned_risk'])} USDT" if e.get("planned_risk") else ""
        protect = "stop-loss and take-profit placed on Binance" if ev == "entry_protected" else "simulated"
        return a(GREEN, f"bought {_n(e.get('qty'), 6)} {sym} at {_n(e.get('price'))} "
                        f"({e.get('strategy', '?')}; {protect}{risk})", f"{ev}:{sym}")
    if ev == "trade_closed":
        if "pnl" in e:                                            # paper / forward account
            return a(GREEN, f"closed {sym} ({e.get('exit_reason', '?')}): bought {_n(e.get('entry_price'))}, "
                            f"sold {_n(e.get('exit_price'))}, P&L {_n(e['pnl'])} USDT after fees", f"{ev}:{sym}")
        sold = f", sold at {_n(e['sold_at'])}" if e.get("sold_at") else ""
        return a(GREEN, f"closed {sym} ({e.get('why', '?')}): bought {_n(e.get('entry'))}{sold}", f"{ev}:{sym}")
    if ev == "ledger_update" and e.get("status") == "CLOSED":
        return a(GREEN, f"{sym} trade {e.get('trade', '')} {e.get('note', 'closed')}", f"{ev}:{e.get('trade')}")
    if ev in ("unprotected", "entry_unprotected"):
        return a(RED, f"ACT NOW: coins may be unprotected. {e.get('detail') or e.get('reason', '')}",
                 f"{ev}:{e.get('detail') or e.get('reason')}")
    if ev == "entry_emergency_exit":
        return a(RED, f"{sym}: bought, but the protection was not confirmed, so the coins were sold at market. "
                      f"Entries are blocked. {e.get('reason', '')}")
    if ev == "entry_unknown":
        return a(RED, f"{sym}: an order's outcome is unknown; reconciliation will settle it and entries are "
                      f"paused meanwhile. {e.get('reason', '')}")
    if ev == "reconciliation_mismatch":
        return a(RED, f"the account does not match the ledger; entries blocked. {e.get('detail', '')}",
                 f"{ev}:{e.get('detail')}")
    if ev == "exit_failed":
        return a(RED, f"ACT NOW if this repeats: could not close {sym} ({e.get('why', '')}): {e.get('reason', '')}",
                 f"{ev}:{sym}")
    if ev == "breaker":
        return a(RED, f"breaker tripped: {e.get('breach')}; entries blocked", f"{ev}:{e.get('breach')}")
    if ev == "mode_changed":
        to = e.get("to_mode")
        if to == "RUNNING":
            return a(GREEN, f"entries allowed again (resumed by {e.get('actor')}: {e.get('reason')})")
        return a(YELLOW, f"mode {to} ({e.get('actor')}: {e.get('reason')})", f"{ev}:{to}:{e.get('reason')}")
    if ev == "fault_cleared":
        return a(GREEN, f"{e.get('fault')} cleared; entries stay blocked until you resume")
    if ev == "started":
        return a(YELLOW, "runner started; new entries are blocked until you resume")
    if ev == "stopped":
        return a(YELLOW, "runner stopped")
    if ev == "entry_blocked":
        return a(YELLOW, f"{e.get('strategy', '?')} wanted to buy {sym}, but entries are blocked "
                         f"({e.get('mode')}{'; ' + e['reason'] if e.get('reason') else ''})", f"{ev}:{sym}")
    if ev in ("strategy_error", "mark_failed", "request_failed", "resume_refused", "decisions_missed",
              "entry_refused"):
        detail = e.get("reason") or (f"{e.get('candles')} candles" if ev == "decisions_missed" else "")
        return a(YELLOW, f"{ev.replace('_', ' ')}: {detail}")
    if ev in _DELAYED:                                           # reported only if it lasts (see Watcher)
        return a(YELLOW, f"{ev.replace('_', ' ')} for over {PAUSE_GRACE_S // 60} minutes: {e.get('reason', '')}")
    return None


# ---------------------------------------------------------------- reading the journals
@dataclass
class Watcher:
    """Follows the journals and turns new lines into alerts. Its own state (where it got to in each journal,
    what it held back) is saved, so a restart neither repeats nor misses alerts."""
    home: Path
    state_dir: Path
    sources: dict[str, str] = field(default_factory=lambda: dict(SOURCES))
    offsets: dict[str, int] = field(default_factory=dict)
    last_seen: dict[str, float] = field(default_factory=dict)       # time of the newest line per source
    last_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent_at: dict[str, float] = field(default_factory=dict)         # throttle: alert key -> last sent
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # delayed alerts waiting for recovery
    silent: dict[str, bool] = field(default_factory=dict)
    heartbeat_day: str = ""
    notes: list[str] = field(default_factory=list)

    def path(self, source: str) -> Path:
        return Path(self.home) / self.sources[source] / JOURNAL

    # ------------------------------------------------------------ state
    @staticmethod
    def open(home: Path, state_dir: Path, now: float, sources: dict[str, str] | None = None) -> "Watcher":
        w = Watcher(Path(home), Path(state_dir), dict(sources or SOURCES))
        f = w.state_dir / STATE_FILE
        try:
            d = json.loads(f.read_text()) if f.exists() else None
        except (OSError, ValueError) as e:
            d = None
            w.notes.append(f"the alert state {f} was unreadable ({type(e).__name__}); starting from now")
        if d is None:
            for s in w.sources:                                       # never replay old history
                p = w.path(s)
                w.offsets[s] = p.stat().st_size if p.exists() else 0
                w.last_seen[s] = p.stat().st_mtime if p.exists() else now
            return w
        w.offsets = {k: int(v) for k, v in d.get("offsets", {}).items()}
        w.last_seen = {k: float(v) for k, v in d.get("last_seen", {}).items()}
        w.last_status, w.sent_at = d.get("last_status", {}), d.get("sent_at", {})
        w.pending, w.silent, w.heartbeat_day = d.get("pending", {}), d.get("silent", {}), d.get("heartbeat_day", "")
        return w

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = {"offsets": self.offsets, "last_seen": self.last_seen, "last_status": self.last_status,
                "sent_at": self.sent_at, "pending": self.pending, "silent": self.silent,
                "heartbeat_day": self.heartbeat_day}
        tmp = self.state_dir / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, self.state_dir / STATE_FILE)

    # ------------------------------------------------------------ one pass
    def poll(self, now: float) -> list[Alert]:
        out = [Alert(YELLOW, "alerts", n, "note") for n in self.notes]
        self.notes = []
        for source in self.sources:
            for e in self._new_events(source, now):
                out += self._handle(source, e, now)
            out += self._check_silence(source, now)
        out += self._due_delayed(now)
        out += self._heartbeat(now)
        self.sent_at = {k: t for k, t in self.sent_at.items() if now - t < 86_400}
        return [a for a in out if self._let_through(a, now)]

    def _new_events(self, source: str, now: float) -> list[dict[str, Any]]:
        p = self.path(source)
        if not p.exists():
            return []
        size = p.stat().st_size
        start = self.offsets.get(source, 0)
        if size < start:                                          # replaced or truncated: start again
            start = 0
        if size == start:
            return []
        with open(p, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
        end = chunk.rfind(b"\n")
        if end < 0:                                               # a line still being written
            return []
        self.offsets[source] = start + end + 1
        self.last_seen[source] = now
        events = []
        for raw in chunk[:end].splitlines():
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        return events

    def _handle(self, source: str, e: dict[str, Any], now: float) -> list[Alert]:
        ev = e.get("event", "")
        if ev == "status":
            self.last_status[source] = {k: e.get(k) for k in ("at", "equity", "mode", "drawdown_pct")}
            return []
        for start, end in _DELAYED.items():
            if ev == start:
                self.pending.setdefault(f"{source}:{start}", {"at": now, "event": e})
                return []
            if ev == end:
                held = self.pending.pop(f"{source}:{start}", None)
                if held is not None and held.get("sent"):
                    return [Alert(GREEN, source, f"{start.replace('_', ' ')} is over", f"{end}")]
                return []
        a = describe(source, e)
        return [a] if a else []

    def _due_delayed(self, now: float) -> list[Alert]:
        out = []
        for key, held in self.pending.items():
            if not held.get("sent") and now - held["at"] >= PAUSE_GRACE_S:
                held["sent"] = True
                a = describe(key.split(":", 1)[0], held["event"])
                if a:
                    out.append(a)
        return out

    def _check_silence(self, source: str, now: float) -> list[Alert]:
        if not self.path(source).exists():
            return []
        quiet = now - self.last_seen.get(source, now)
        if quiet >= SILENT_S and not self.silent.get(source):
            self.silent[source] = True
            return [Alert(RED, source, f"no sign of life for {int(quiet // 60)} minutes: is the service stopped "
                                       f"or crashed? (as root: systemctl status bot-{source})", "silent")]
        if quiet < SILENT_S and self.silent.get(source):
            self.silent[source] = False
            return [Alert(GREEN, source, "running again", "back")]
        return []

    def _heartbeat(self, now: float) -> list[Alert]:
        t = datetime.fromtimestamp(now, timezone.utc)
        day = t.strftime("%Y-%m-%d")
        if t.hour < HEARTBEAT_HOUR_UTC or self.heartbeat_day == day:
            return []
        self.heartbeat_day = day
        parts = []
        for s in self.sources:
            st = self.last_status.get(s)
            if not self.path(s).exists():
                continue
            if st:
                parts.append(f"{s} {st.get('mode')}, equity {_n(st.get('equity'))}")
            else:
                parts.append(f"{s}: no status yet")
        return [Alert(GREEN, "daily check", "; ".join(parts) or "no runner journals found", f"heartbeat:{day}")]

    def _let_through(self, a: Alert, now: float) -> bool:
        if a.level == GREEN:
            return True
        key = f"{a.source}:{a.key}"
        wait = REPEAT_S if a.level == RED else THROTTLE_S
        last = self.sent_at.get(key)
        if last is not None and now - last < wait:
            return False
        self.sent_at[key] = now
        return True


# ---------------------------------------------------------------- Telegram
class TelegramError(RuntimeError):
    pass


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class Telegram:
    token: str = field(repr=False)
    chat_id: str = ""
    transport: Transport | None = None

    def __repr__(self) -> str:                       # never show the token, even in tracebacks
        return f"Telegram(chat_id={self.chat_id!r})"

    def call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            body = (self.transport or _https)(f"{API}/bot{self.token}/{method}", params)
        except TelegramError as e:
            raise TelegramError(self._scrub(str(e))) from None
        except Exception as e:
            raise TelegramError(self._scrub(f"{type(e).__name__}: {e}")) from None
        if not body.get("ok"):
            raise TelegramError(self._scrub(f"Telegram refused {method}: {body.get('description', '?')}"))
        return body.get("result")

    def send(self, text: str) -> None:
        self.call("sendMessage", {"chat_id": self.chat_id, "text": text[:4096], "disable_web_page_preview": "true"})

    def _scrub(self, s: str) -> str:
        return s.replace(self.token, "<token>") if self.token else s


def _https(url: str, params: dict[str, Any]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise TelegramError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise TelegramError(f"network: {type(e).__name__}") from None


def messages(lines: list[str]) -> list[str]:
    """Lines packed into as few Telegram messages as fit."""
    out, cur = [], ""
    for line in lines:
        line = line[:MAX_MESSAGE]
        if cur and len(cur) + 1 + len(line) > MAX_MESSAGE:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- the service
@dataclass
class AlertService:
    watcher: Watcher
    telegram: Telegram
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = print
    queue: list[str] = field(default_factory=list)
    stop_requested: bool = False

    def tick(self) -> None:
        now = self.clock()
        self.queue += [a.line() for a in self.watcher.poll(now)]
        self.watcher.save()
        if len(self.queue) > MAX_QUEUE:
            dropped = len(self.queue) - MAX_QUEUE
            self.queue = [f"{YELLOW} alerts: {dropped} older alerts were dropped while Telegram was unreachable"] \
                + self.queue[-MAX_QUEUE:]
        for msg in messages(self.queue):
            try:
                self.telegram.send(msg)
            except TelegramError as e:
                self.log(f"telegram: not sent, will retry: {e}")
                return
            n = msg.count("\n") + 1
            self.queue = self.queue[n:]
            self.log(f"telegram: sent {n} alert(s)")

    def run(self, interval_s: float = 30.0, max_ticks: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        ticks = 0
        while not self.stop_requested and (max_ticks is None or ticks < max_ticks):
            self.tick()
            ticks += 1
            if not self.stop_requested and (max_ticks is None or ticks < max_ticks):
                sleep(interval_s)
