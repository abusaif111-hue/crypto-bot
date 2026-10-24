"""Module 6 (part): the private dashboard. Reads the runners' files, never writes or trades, escapes
everything it shows, answers GET only and listens on this machine only."""
import ast
import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import dashboard as db

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
NOW = 1_792_830_000.0


def jl(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def home(tmp_path):
    live = tmp_path / "live"
    jl(live / db.JOURNAL, {"at": NOW - 120, "event": "status", "equity": "312.50", "drawdown_pct": "0.4",
                           "mode": "RUNNING"},
       {"at": NOW - 60, "event": "entry_protected", "symbol": "BTCUSDT", "strategy": "H2b", "reason": "filled"})
    (live / db.ACCOUNT).write_text(json.dumps({"capital": "300", "modes": {"mode": "RUNNING", "active_breaches": []}}))
    (live / db.LEDGER).write_text(json.dumps({"trades": [
        {"symbol": "BTCUSDT", "status": "OPEN", "received_qty": "0.0012", "entry_price": "80000",
         "stop_price": "74000", "take_profit_price": "98000", "created_at": NOW - 60},
        {"symbol": "ETHUSDT", "status": "CLOSED"}, {"symbol": "XRPUSDT", "status": "NOT_OPENED"}]}))
    paper = tmp_path / "paper"
    jl(paper / db.JOURNAL, {"at": NOW - 30, "event": "status", "equity": "9994.79", "mode": "RUNNING"})
    (paper / db.STATE).write_text(json.dumps({
        "starting_quote": "10000", "modes": {"mode": "RUNNING", "active_breaches": []},
        "positions": [{"symbol": "BTCUSDT", "qty": "0.0096", "entry_price": "81754.87", "stop_price": "80000",
                       "take_profit_price": "87000", "opened_at": NOW - 86400}],
        "trades": [{"pnl": "-1.5"}, {"pnl": "4.0"}]}))
    return tmp_path


def test_it_reads_each_runner_from_its_own_files(home):
    live, testnet, forward, paper = db.read_all(home)
    assert live.found and live.equity == Dec("312.50") and live.started_with == Dec("300")
    assert [t["symbol"] for t in live.open_trades] == ["BTCUSDT"] and live.closed == 1
    assert live.recent[0]["event"] == "entry_protected"
    assert not testnet.found and not forward.found
    assert paper.started_with == Dec("10000") and paper.closed == 2 and paper.closed_pnl == Dec("2.5")
    assert paper.open_trades[0]["entry"] == "81754.87"


def test_the_page_shows_real_money_trades_and_breakers(home):
    (home / "live" / db.ACCOUNT).write_text(json.dumps({"capital": "300", "modes": {
        "mode": "ENTRIES_BLOCKED", "active_breaches": ["REJ_DRAWDOWN_BREAKER"]}}))
    page = db.render(db.read_all(home), NOW)
    assert "REAL money" in page and "312.50" in page and "+12.50, +4.17%" in page
    assert "REJ_DRAWDOWN_BREAKER" in page and "ENTRIES_BLOCKED" in page
    assert "BTCUSDT" in page and "74,000.00" in page and "Not set up on this server" in page
    assert f'content="{db.REFRESH_S}"' in page


def test_a_silent_runner_is_flagged(home):
    page = db.render(db.read_all(home), NOW + db.SILENT_S + 1)
    assert "silent: is the service down?" in page


def test_a_broken_file_is_reported_not_a_crash(home):
    (home / "live" / db.LEDGER).write_text("{broken")
    live = db.read_all(home)[0]
    assert any("ledger.json is unreadable" in p for p in live.problems)
    assert "unreadable" in db.render([live], NOW)


def test_everything_from_a_file_is_escaped(home):
    jl(home / "live" / db.JOURNAL, {"at": NOW, "event": "exit_failed", "symbol": "<script>alert(1)</script>",
                                    "reason": "\"><img src=x onerror=alert(1)>"})
    page = db.render(db.read_all(home), NOW)
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_it_never_writes_reads_secrets_or_trades():
    text = (SRC / "dashboard.py").read_text()
    tree = ast.parse(text)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("bot") for m in mods), mods
    assert not any(m and m.startswith(("urllib", "requests", "socket", "binance")) for m in mods), mods
    for bad in ('".env"', "'.env'", "write_text", "unlink", "os.replace", '"w"', "'w'", '"a"'):
        assert bad not in text, bad


def test_it_listens_on_this_machine_only(home):
    for host in ("0.0.0.0", "167.71.59.188", "::"):
        with pytest.raises(ValueError, match="this machine only"):
            db.serve(home, host, 0)


def test_the_server_answers_get_only(home):
    server = db.serve(home, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "REAL money" in body
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/health").read() == b"ok\n"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"x", method="POST"))
        assert e.value.code == 405
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../.env")
        assert e.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_drawdown_comes_from_the_saved_index_when_the_status_has_none(home):
    paper_state = json.loads((home / "paper" / db.STATE).read_text())
    paper_state["index"] = {"units": "10000", "value_per_unit": "0.9777", "peak_value_per_unit": "1.0002"}
    (home / "paper" / db.STATE).write_text(json.dumps(paper_state))
    live_acct = json.loads((home / "live" / db.ACCOUNT).read_text())
    live_acct["index"] = {"value_per_unit": "0.5", "peak_value_per_unit": "1"}       # the status line wins
    (home / "live" / db.ACCOUNT).write_text(json.dumps(live_acct))
    live, _, _, paper = db.read_all(home)
    assert live.drawdown_pct == Dec("0.4")
    assert abs(paper.drawdown_pct - (1 - Dec("0.9777") / Dec("1.0002")) * 100) < Dec("0.0001")
    paper_state["index"] = {"value_per_unit": "1.1", "peak_value_per_unit": "1.1"}
    (home / "paper" / db.STATE).write_text(json.dumps(paper_state))
    assert db.read_all(home)[3].drawdown_pct == 0
