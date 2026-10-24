"""Private dashboard (module 6): one page on the server showing every account the bot trades.

It reads the files the runners already write (account, ledger, saved state and journal of the live
runner, the testnet runner, the H2b forward test and paper trading) and shows each one's mode,
breakers, equity, open trades and recent activity. It refreshes itself every minute.

Read-only and private by design:
- it imports nothing from `bot` and never talks to Binance, so it cannot place, change or cancel
  anything, and it never reads `.env` or any key;
- it answers GET only, and listens on 127.0.0.1 only (`serve` refuses any other address). Sultan opens
  it through an SSH tunnel from his own computer; it is never on the public internet;
- every value from a file is HTML-escaped before it is shown.

Python's own HTTP server is used, not a web framework: no new dependency on a 512 MB server.
"""
from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# (name shown, folder under the bot user's home, what the money is)
SOURCES = (("live", "live", "REAL money"), ("testnet", "testnet", "testnet money"),
           ("forward", "forward-h2b", "simulated"), ("paper", "paper", "simulated"))
JOURNAL, ACCOUNT, LEDGER, STATE = "journal.jsonl", "account.json", "ledger.json", "state.json"
TAIL_BYTES = 262_144                   # the end of each journal is enough for the page
SILENT_S = 2400                        # no journal line for 40 minutes: the runner may be down
RECENT = 12
REFRESH_S = 60
LOOPBACK = ("127.0.0.1", "localhost", "::1")
NOTABLE = {
    "started", "stopped", "entry_protected", "entry_filled", "entry_refused", "entry_emergency_exit",
    "entry_unprotected", "entry_unknown", "entry_blocked", "trade_closed", "unprotected",
    "reconciliation_mismatch", "exit_failed", "breaker", "mode_changed", "fault_cleared", "entries_paused",
    "entries_unpaused", "strategy_error", "decisions_missed", "mark_failed", "market_data_down",
    "market_data_back", "resume_refused",
}
BAD = {"entry_emergency_exit", "entry_unprotected", "entry_unknown", "unprotected", "reconciliation_mismatch",
       "exit_failed", "breaker"}


@dataclass
class Account:
    name: str
    money: str
    found: bool = False
    mode: str = "?"
    breaches: list[str] = field(default_factory=list)
    equity: Decimal | None = None
    started_with: Decimal | None = None
    drawdown_pct: Decimal | None = None
    status_at: float | None = None
    last_line_at: float | None = None
    open_trades: list[dict[str, Any]] = field(default_factory=list)
    closed: int = 0
    closed_pnl: Decimal | None = None
    recent: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _d(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _json(path: Path, acct: Account) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        acct.problems.append(f"{path.name} is unreadable ({type(e).__name__})")
        return None


def _journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL_BYTES))
        chunk = f.read()
    lines = chunk.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]                                    # the first line may be cut
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _index_drawdown(saved: dict[str, Any]) -> Decimal | None:
    """Drawdown in % from a saved account's drawdown index, for runners whose status line has none."""
    ix = saved.get("index") or {}
    value, peak = _d(ix.get("value_per_unit")), _d(ix.get("peak_value_per_unit"))
    if value is None or not peak or peak <= 0:
        return None
    return max(Decimal(0), (1 - value / peak) * 100)


def read_account(home: Path, name: str, folder: str, money: str) -> Account:
    """Everything the page shows about one runner, from its files. Never raises for a bad file."""
    d = Path(home) / folder
    acct = Account(name, money, found=d.is_dir())
    if not acct.found:
        return acct
    events = _journal(d / JOURNAL)
    if events:
        acct.last_line_at = float(events[-1].get("at") or 0) or None
    status = next((e for e in reversed(events) if e.get("event") == "status"), None)
    if status:
        acct.status_at = status.get("at")
        acct.equity = _d(status.get("equity"))
        acct.drawdown_pct = _d(status.get("drawdown_pct"))
        acct.mode = str(status.get("mode") or "?")
    acct.recent = [e for e in events if e.get("event") in NOTABLE][-RECENT:][::-1]

    account = _json(d / ACCOUNT, acct)
    if isinstance(account, dict):                            # an exchange runner (live, testnet)
        acct.started_with = _d(account.get("capital"))
        modes = account.get("modes") or {}
        acct.mode = str(modes.get("mode") or acct.mode)
        acct.breaches = list(modes.get("active_breaches") or [])
        if acct.drawdown_pct is None:
            acct.drawdown_pct = _index_drawdown(account)
        ledger = _json(d / LEDGER, acct) or {}
        for t in ledger.get("trades", []):
            if t.get("status") in ("OPEN", "PENDING"):
                acct.open_trades.append({"symbol": t.get("symbol"), "status": t.get("status"),
                                         "qty": t.get("received_qty"), "entry": t.get("entry_price"),
                                         "stop": t.get("stop_price"), "target": t.get("take_profit_price"),
                                         "since": t.get("created_at")})
            elif t.get("status") == "CLOSED":
                acct.closed += 1
        return acct
    state = _json(d / STATE, acct)
    if isinstance(state, dict):                              # a simulated account (paper, forward test)
        acct.started_with = _d(state.get("starting_quote"))
        modes = state.get("modes") or {}
        acct.mode = str(modes.get("mode") or acct.mode)
        acct.breaches = list(modes.get("active_breaches") or [])
        if acct.drawdown_pct is None:                        # the paper runner's status line has no drawdown
            acct.drawdown_pct = _index_drawdown(state)
        for p in state.get("positions", []):
            acct.open_trades.append({"symbol": p.get("symbol"), "status": "OPEN", "qty": p.get("qty"),
                                     "entry": p.get("entry_price"), "stop": p.get("stop_price"),
                                     "target": p.get("take_profit_price"), "since": p.get("opened_at")})
        trades = state.get("trades", [])
        acct.closed = len(trades)
        pnls = [_d(t.get("pnl")) for t in trades]
        acct.closed_pnl = sum((p for p in pnls if p is not None), Decimal(0)) if trades else None
    return acct


def read_all(home: Path) -> list[Account]:
    return [read_account(home, n, f, m) for n, f, m in SOURCES]


# ---------------------------------------------------------------- the page
def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _money(v: Decimal | None) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _num(v: Any) -> str:
    d = _d(v)
    if d is None:
        return "—"
    return f"{d:,.2f}" if abs(d) >= 1 else f"{d.normalize():f}"


def _when(t: Any, now: float) -> str:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "—"
    ago = max(0, now - t)
    rel = f"{int(ago)} s" if ago < 120 else f"{int(ago // 60)} min" if ago < 7200 else f"{ago / 3600:.1f} h"
    return f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(t))} UTC ({rel} ago)"


def _detail(e: dict[str, Any]) -> str:
    keys = ("symbol", "strategy", "why", "reason", "detail", "breach", "fault", "to_mode", "actor", "note", "status")
    return "; ".join(f"{k} {e[k]}" for k in keys if e.get(k) not in (None, "", []))[:300]


def _card(a: Account, now: float) -> str:
    if not a.found:
        return (f'<section class="card"><h2>{_e(a.name)}</h2><p class="muted">Not set up on this server.</p>'
                f"</section>")
    alive = a.last_line_at is not None and now - a.last_line_at < SILENT_S
    health = ('<span class="pill ok">running</span>' if alive else
              '<span class="pill bad">silent: is the service down?</span>')
    mode_cls = "ok" if a.mode == "RUNNING" and not a.breaches else "warn"
    change = ""
    if a.equity is not None and a.started_with:
        diff = a.equity - a.started_with
        cls = "pos" if diff > 0 else "neg" if diff < 0 else ""
        change = f' <span class="{cls}">({diff:+,.2f}, {diff / a.started_with * 100:+.2f}%)</span>'
    rows = "".join(
        f"<tr><td>{_e(t['symbol'])}</td><td>{_e(t['status'])}</td><td class=n>{_e(_num(t['qty']))}</td>"
        f"<td class=n>{_e(_num(t['entry']))}</td><td class=n>{_e(_num(t['stop']))}</td>"
        f"<td class=n>{_e(_num(t['target']))}</td><td>{_e(_when(t['since'], now))}</td></tr>" for t in a.open_trades)
    trades = (f'<table><tr><th>Coin</th><th>Status</th><th class=n>Qty</th><th class=n>Bought</th><th class=n>Stop</th>'
              f'<th class=n>Target</th><th>Since</th></tr>{rows}</table>' if a.open_trades
              else '<p class="muted">No open trades.</p>')
    recent = "".join(
        f'<li class="{"bad" if e.get("event") in BAD else ""}"><span class="muted">{_e(_when(e.get("at"), now))}</span> '
        f'<b>{_e(e.get("event"))}</b> {_e(_detail(e))}</li>' for e in a.recent) or '<li class="muted">Nothing yet.</li>'
    problems = "".join(f'<p class="bad">{_e(p)}</p>' for p in a.problems)
    breaches = f' <span class="pill bad">{_e(", ".join(a.breaches))}</span>' if a.breaches else ""
    pnl = f" · closed P&amp;L {_e(_money(a.closed_pnl))} USDT" if a.closed_pnl is not None else ""
    return f"""<section class="card">
<h2>{_e(a.name)} <span class="pill {'real' if a.money == 'REAL money' else ''}">{_e(a.money)}</span> {health}</h2>
{problems}
<p class="big">{_e(_money(a.equity))} <small>USDT</small>{change}</p>
<p>Mode <span class="pill {mode_cls}">{_e(a.mode)}</span>{breaches} · started with {_e(_money(a.started_with))} USDT
 · drawdown {_e(_num(a.drawdown_pct)) if a.drawdown_pct is not None else "—"}% · {a.closed} closed trades{pnl}</p>
<p class="muted">Equity as of {_e(_when(a.status_at, now))}</p>
<h3>Open trades</h3>{trades}
<h3>Recent activity</h3><ul>{recent}</ul>
</section>"""


REPORT = Path("reports") / "latest.md"


def read_report(home: Path) -> str | None:
    """The latest daily report (bot report), or None when there is none yet."""
    try:
        return (Path(home) / REPORT).read_text()[:200_000]
    except OSError:
        return None


def _report_card(text: str | None) -> str:
    if text is None:
        return '<section class="card"><h2>Daily report</h2><p class="muted">No report yet (bot report).</p></section>'
    head = text.splitlines()[:2]
    return (f'<section class="card"><h2>Daily report</h2><p>{"<br>".join(_e(x) for x in head)}</p>'
            f'<p><a href="/report">The full report</a></p></section>')


def render(accounts: list[Account], now: float, report: str | None = None) -> str:
    cards = _report_card(report) + "".join(_card(a, now) for a in accounts)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}"><title>Crypto bot</title>
<style>
:root {{ --bg:#f6f6f4; --fg:#1f1f1d; --muted:#6b6b66; --card:#fff; --line:#e3e2dc; --ok:#2e7d4f; --warn:#a86b00;
  --bad:#b3261e; --real:#7a2bd6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1a1a18; --fg:#ecebe6; --muted:#a3a29b; --card:#24241f;
  --line:#3a3a33; --ok:#6fcf97; --warn:#f2c14e; --bad:#ff8a80; --real:#c39bff; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }}
main {{ max-width:1100px; margin:auto; display:grid; gap:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; overflow-x:auto; }}
h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:18px; margin:0 0 8px; }} h3 {{ font-size:14px; margin:14px 0 6px; }}
.big {{ font-size:28px; margin:4px 0; }} .muted {{ color:var(--muted); }} .pos {{ color:var(--ok); }}
.neg, .bad {{ color:var(--bad); }} small {{ font-size:14px; color:var(--muted); }}
.pill {{ font-size:12px; border:1px solid var(--line); border-radius:99px; padding:1px 8px; white-space:nowrap; }}
.pill.ok {{ color:var(--ok); }} .pill.warn {{ color:var(--warn); }} .pill.bad {{ color:var(--bad); }}
.pill.real {{ color:var(--real); font-weight:600; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
.n {{ text-align:right; }} ul {{ margin:0; padding-left:18px; }} li {{ margin:2px 0; }}
</style></head>
<body><main>
<header><h1>Crypto bot</h1><p class="muted">Read-only view of the server's own files · {_e(_when(now, now))} ·
refreshes every {REFRESH_S} s. To change anything, use the console (live / testnet / paper / forward commands).</p></header>
{cards}
</main></body></html>"""


# ---------------------------------------------------------------- the server
def make_handler(home: Path, clock=time.time) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "crypto-bot-dashboard"

        def do_GET(self) -> None:                          # noqa: N802 (http.server's name)
            if self.path.split("?")[0] == "/health":
                body, ctype, code = b"ok\n", "text/plain; charset=utf-8", 200
            elif self.path.split("?")[0] in ("/", "/index.html"):
                body = render(read_all(home), clock(), read_report(home)).encode()
                ctype, code = "text/html; charset=utf-8", 200
            elif self.path.split("?")[0] == "/report":
                body = (read_report(home) or "No report yet (bot report).\n").encode()
                ctype, code = "text/plain; charset=utf-8", 200
            else:
                body, ctype, code = b"not found\n", "text/plain; charset=utf-8", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: the page refreshes every minute
            pass

    return Handler


def serve(home: Path, host: str = "127.0.0.1", port: int = 8050) -> ThreadingHTTPServer:
    """The server, bound to this machine only. Call `serve_forever()` on it."""
    if host not in LOOPBACK:
        raise ValueError(f"the dashboard listens on this machine only (127.0.0.1), not {host!r}; "
                         f"open it through an SSH tunnel")
    return ThreadingHTTPServer((host, port), make_handler(Path(home)))
