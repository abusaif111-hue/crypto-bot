"""The risk monitor (docs/intel-plan.md, section 7): read-only findings across accounts and coins."""
import argparse
import ast
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli, monitor
from bot.core.config import AppConfig
from bot.exchange.interface import Book
from tests.unit.test_h2b import path
from tests.unit.test_signal_report import four_hourly

SRC = Path(__file__).resolve().parents[2] / "src" / "bot" / "monitor.py"
CFG = AppConfig()
NOW = 1_792_830_000.0                       # 2026-10-24 (a Saturday) 03:40 UTC
TODAY = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
GROUPS = {"BTCUSDT": "majors", "ETHUSDT": "majors", "XRPUSDT": "XRPUSDT", "BNBUSDT": "BNBUSDT"}


def jl(p, *events):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def periods(day_start="100", week_start="100", day_anchor=TODAY):
    return {"day_anchor": day_anchor, "week_anchor": TODAY, "day_start_equity": day_start,
            "week_start_equity": week_start, "day_flows": "0", "week_flows": "0"}


def live_account(home, breaches=(), mode="RUNNING", **p):
    (home / "live" / "account.json").write_text(json.dumps(
        {"capital": "100", "periods": periods(**p), "modes": {"mode": mode, "active_breaches": list(breaches)}}))


def status(home, folder="live", equity="100", drawdown="0.00", at=NOW - 60):
    jl(home / folder / "journal.jsonl", {"at": at, "event": "status", "equity": equity, "drawdown_pct": drawdown,
                                         "mode": "RUNNING"})


def trades(home, *open_trades):
    (home / "live" / "ledger.json").write_text(json.dumps({"trades": [
        {"symbol": s, "status": "OPEN", "received_qty": q, "entry_price": e, "stop_price": "1",
         "take_profit_price": "9"} for s, q, e in open_trades]}))


@pytest.fixture
def home(tmp_path):
    (tmp_path / "live").mkdir()
    live_account(tmp_path)
    trades(tmp_path)
    status(tmp_path)
    fwd = tmp_path / "forward-h2b"
    fwd.mkdir()
    (fwd / "forward.json").write_text(json.dumps({"started_at": NOW - 10 * 86400, "symbols": list(GROUPS),
                                                  "corr_groups": GROUPS}))
    (fwd / "state.json").write_text(json.dumps({"starting_quote": "10000", "periods": periods("10000", "10000"),
                                                "modes": {"mode": "RUNNING", "active_breaches": []},
                                                "positions": [], "trades": [{"pnl": "1"}]}))
    status(tmp_path, "forward-h2b", equity="10001")
    return tmp_path


def rules(found):
    return [(f.level, f.rule, f.subject) for f in found]


def acct(home, now=NOW):
    return monitor.account_findings(home, now, CFG, GROUPS)


# ---------------------------------------------------------------- accounts
def test_a_healthy_bot_has_only_the_evidence_note(home):
    found = acct(home)
    assert rules(found) == [("info", "insufficient evidence", "forward")]
    assert found[0].data == {"closed_trades": "1", "needed_trades": "30", "days": "10", "needed_days": "365"}
    text = monitor.render(found, NOW)
    assert "0 critical, 0 warnings, 1 notes." in text and "No warnings." in text
    assert monitor.LIMITS in text and "gap through a stop" in text


def test_a_silent_runner_is_a_critical_outage(home):
    found = acct(home, NOW + monitor.SILENT_S + 60)
    (live,) = [f for f in found if f.subject == "live"]
    assert (live.level, live.rule) == ("critical", "data outage") and live.data["minutes_silent"] == "42"


def test_a_failed_price_read_is_an_outage_until_a_status_follows(home):
    jl(home / "live" / "journal.jsonl", {"at": NOW - 30, "event": "mark_failed", "reason": "timeout"})
    (f,) = [f for f in acct(home) if f.subject == "live"]
    assert (f.level, f.rule, f.data["detail"]) == ("critical", "data outage", "timeout")
    status(home, at=NOW - 10)
    assert not [f for f in acct(home) if f.subject == "live"]


def test_a_reconciliation_mismatch_is_critical_with_its_detail(home):
    jl(home / "live" / "journal.jsonl", {"at": NOW - 20, "event": "reconciliation_mismatch", "detail": "BTC differs"})
    live_account(home, breaches=["RECONCILIATION_MISMATCH"], mode="ENTRIES_BLOCKED")
    (f,) = [f for f in acct(home) if f.subject == "live"]
    assert (f.level, f.rule) == ("critical", "inconsistent balances") and f.data["detail"] == "BTC differs"


@pytest.mark.parametrize("dd,level", [("2.99", None), ("3.00", "warning"), ("5.99", "warning"), ("6.00", "critical")])
def test_drawdown_warns_at_half_the_breaker_and_is_critical_at_it(home, dd, level):
    status(home, drawdown=dd, at=NOW - 5)
    got = [f.level for f in acct(home) if f.rule == "drawdown"]
    assert got == ([level] if level else [])


def test_a_tripped_drawdown_breaker_is_reported_once(home):
    live_account(home, breaches=["REJ_DRAWDOWN_BREAKER"])
    status(home, drawdown="6.20", at=NOW - 5)
    assert [f.level for f in acct(home) if f.rule == "drawdown"] == ["critical"]


def test_other_breakers_are_warnings(home):
    live_account(home, breaches=["REJ_DAILY_LOSS_LIMIT"])
    assert ("warning", "breaker", "live") in rules(acct(home))


@pytest.mark.parametrize("equity,daily,weekly", [("99.30", False, False), ("99.25", True, False), ("98.40", True, True)])
def test_daily_and_weekly_losses_warn_at_half_their_limits(home, equity, daily, weekly):
    status(home, equity=equity, at=NOW - 5)
    got = {f.rule for f in acct(home)}
    assert ("daily loss" in got) == daily and ("weekly loss" in got) == weekly


def test_the_loss_counts_deposits_and_withdrawals_as_flows(home):
    p = periods("100", "100")
    p["day_flows"] = p["week_flows"] = "-5"                 # 5 USDT taken out today: not a loss
    (home / "live" / "account.json").write_text(json.dumps({"capital": "100", "periods": p,
                                                            "modes": {"mode": "RUNNING", "active_breaches": []}}))
    status(home, equity="94.20", at=NOW - 5)                 # 100 - 5 - 94.20 = 0.80 lost
    (f,) = [f for f in acct(home) if f.rule == "daily loss"]
    assert f.data == {"loss_pct": "0.8", "limit_pct": "1.5"}
    status(home, equity="95.00", at=NOW - 4)
    assert "daily loss" not in {f.rule for f in acct(home)}


def test_no_daily_figure_before_the_runner_rolls_into_today(home):
    live_account(home, day_anchor="2026-10-23T00:00:00+00:00")
    status(home, equity="99.00", at=NOW - 5)
    assert "daily loss" not in {f.rule for f in acct(home)}


def test_concentration_by_coin_and_by_correlation_group(home):
    trades(home, ("BTCUSDT", "0.0004", "80000"))                     # 32 USDT of a 100 USDT account
    (f,) = [f for f in acct(home) if f.rule == "concentration"]
    assert f.data["coin"] == "BTCUSDT" and f.data["share_pct"] == "32" and f.data["limit_pct"] == "25"
    trades(home, ("BTCUSDT", "0.0001", "80000"), ("ETHUSDT", "0.001", "2500"), ("ETHUSDT", "0.001", "2500"))
    (f,) = [f for f in acct(home) if f.rule == "concentration"]
    assert f.data == {"group": "majors", "open": "3", "limit": "2"}


def test_repeated_refusals_of_one_coin_are_flagged(home):
    refused = [{"at": NOW - i * 3600, "event": "entry_refused", "symbol": "ETHUSDT"} for i in range(1, 4)]
    old = {"at": NOW - 4 * 86400, "event": "entry_blocked", "symbol": "ETHUSDT"}
    jl(home / "live" / "journal.jsonl", old, *refused)
    status(home, at=NOW - 1)
    assert "repeated signals" not in {f.rule for f in acct(home)}            # 3 in 3 days, and one too old
    jl(home / "live" / "journal.jsonl", {"at": NOW - 10, "event": "entry_skipped", "symbol": "ETHUSDT"})
    (f,) = [f for f in acct(home) if f.rule == "repeated signals"]
    assert f.data == {"coin": "ETHUSDT", "times": "4", "limit": "3"}


def test_the_evidence_note_needs_both_the_trades_and_the_time(home):
    fwd = home / "forward-h2b"
    state = json.loads((fwd / "state.json").read_text())
    state["trades"] = [{"pnl": "1"}] * 30
    (fwd / "state.json").write_text(json.dumps(state))
    assert "insufficient evidence" in {f.rule for f in acct(home)}          # 30 trades, but 10 days
    (fwd / "forward.json").write_text(json.dumps({"started_at": NOW - 400 * 86400}))
    assert "insufficient evidence" not in {f.rule for f in acct(home)}
    state["trades"] = [{"pnl": "1"}] * 29
    (fwd / "state.json").write_text(json.dumps(state))
    assert "insufficient evidence" in {f.rule for f in acct(home)}          # 400 days, but 29 trades


def test_a_damaged_file_is_a_finding_not_a_crash(home):
    (home / "live" / "ledger.json").write_text("{broken")
    assert ("warning", "unreadable file", "live") in rules(acct(home))


# ---------------------------------------------------------------- markets
def at_close(candles):
    return (candles[-1].close_time + 1) / 1000


def calm(days=420, noise=0.01):
    return four_hourly(path([(days, 0.0, noise)]))


def test_fresh_calm_prices_have_no_findings():
    cs = calm()
    assert monitor.market_findings("BTCUSDT", cs, Dec("100"), Dec("100.01"), at_close(cs) + 60, CFG) == []


def test_stale_prices_are_critical():
    cs = calm()
    late = at_close(cs) + 4 * 3600 + monitor.STALE_MARGIN_S + 60
    (f,) = monitor.market_findings("BTCUSDT", cs, Dec("100"), Dec("100.01"), late, CFG)
    assert (f.level, f.rule) == ("critical", "stale prices") and f.data["late_minutes"] == "251"
    assert rules(monitor.market_findings("BTCUSDT", [], None, None, late, CFG)) == [("critical", "stale prices", "BTCUSDT")]


def test_a_wide_spread_is_flagged_at_the_risk_engine_limit():
    cs = calm()
    assert monitor.market_findings("BTCUSDT", cs, Dec("100"), Dec("100.10"), at_close(cs), CFG) == []
    (f,) = monitor.market_findings("BTCUSDT", cs, Dec("100"), Dec("100.20"), at_close(cs), CFG)
    assert (f.rule, f.data["limit_pct"]) == ("abnormal spread", "0.1")


def test_volatility_in_the_top_tenth_of_its_year_is_flagged():
    cs = four_hourly(path([(400, 0.0, 0.01), (30, 0.0, 0.016)]))           # a little above the year's top 10%
    got = [f for f in monitor.market_findings("BTCUSDT", cs, None, None, at_close(cs), CFG) if "top 10%" in f.reason]
    assert len(got) == 1
    vol, level = Dec(got[0].data["vol_pct"]), Dec(got[0].data["top10_from_pct"])
    assert level <= vol < 2 * level


def test_a_daily_move_beyond_three_times_volatility_is_flagged():
    closes = path([(420, 0.0, 0.01)])
    closes[-1] = closes[-2] * 0.85
    cs = four_hourly(closes)
    got = [f for f in monitor.market_findings("BTCUSDT", cs, None, None, at_close(cs), CFG) if "3 x" in f.reason]
    assert len(got) == 1 and Dec(got[0].data["move_pct"]) > 15


def test_short_history_says_so():
    cs = calm(days=100)
    assert rules(monitor.market_findings("BTCUSDT", cs, None, None, at_close(cs), CFG)) == [
        ("info", "insufficient evidence", "BTCUSDT")]


class FakeData:
    def __init__(self, candles, books_fail=False, coin_fail=(), skew=10):
        self.candles, self.books_fail, self.coin_fail = candles, books_fail, coin_fail
        self.venue = argparse.Namespace(clock_skew_ms=lambda: skew)

    def books(self, symbols):
        if self.books_fail:
            raise TimeoutError("book timed out")
        return {s: Book(Dec("100"), Dec("100.01")) for s in symbols}

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        if symbol in self.coin_fail:
            raise ConnectionError("no route")
        assert interval == "4h" and limit == 399 * 6
        return self.candles[-limit:]


def test_market_failures_are_findings_never_crashes():
    cs = calm()
    now = at_close(cs) + 60
    assert monitor.gather_market(FakeData(cs), ["BTCUSDT"], now, CFG) == []
    got = rules(monitor.gather_market(FakeData(cs, books_fail=True, coin_fail=("ETHUSDT",), skew=-1500),
                                      ["BTCUSDT", "ETHUSDT"], now, CFG))
    assert ("critical", "data outage", "binance") in got and ("critical", "data outage", "ETHUSDT") in got
    assert ("critical", "clock", "server") in got


# ---------------------------------------------------------------- records, the command, read-only
def test_findings_are_recorded_with_their_data_and_the_run(home, tmp_path):
    found = monitor.check(home, NOW, CFG, GROUPS)
    p = monitor.record(found, tmp_path / "mon", NOW)
    monitor.record(found, tmp_path / "mon", NOW + 1)
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert len(rows) == 4 and rows[0]["rule"] == "insufficient evidence" and rows[0]["data"]["needed_trades"] == "30"
    assert rows[1] == {"at": NOW, "run": {"critical": 0, "warning": 0, "info": 1}}


def test_findings_are_sorted_critical_first(home):
    status(home, drawdown="3.5", at=NOW - 6)                                          # live: a warning
    jl(home / "forward-h2b" / "journal.jsonl", {"at": NOW - 5, "event": "mark_failed", "reason": "x"})
    assert [(f.level, f.subject) for f in monitor.check(home, NOW, CFG, GROUPS)] == [
        ("critical", "forward"), ("warning", "live"), ("info", "forward")]


def _hashes(home):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(home.rglob("*")) if p.is_file()}


def test_the_command_reads_only_and_records_in_its_own_folder(home, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda p: CFG)
    before = _hashes(home)
    out = io.StringIO()
    args = argparse.Namespace(config="x", home=str(home), symbols=None, no_market=True,
                              record_dir=str(home / "monitor"))
    assert cli._monitor(args, out=out) == 0
    assert "Prices were not checked" in out.getvalue() and "insufficient evidence" in out.getvalue()
    after = _hashes(home)
    assert {k: v for k, v in after.items() if "/monitor/" not in k} == before
    assert any(k.endswith("monitor/findings.jsonl") for k in after)


def test_the_command_reports_a_connection_failure_as_a_finding(home, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda p: CFG)

    def broken(environment):
        raise ConnectionError("dns failed")
    out = io.StringIO()
    args = argparse.Namespace(config="x", home=str(home), symbols=None, no_market=False,
                              record_dir=str(home / "monitor"))
    assert cli._monitor(args, make_exchange=broken, out=out) == 0
    assert "could not connect to Binance" in out.getvalue() and "CRITICAL" in out.getvalue()


def test_the_command_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_monitor", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["monitor", "--no-market"])
    assert seen["args"].no_market and seen["args"].record_dir == "~/monitor"


def test_the_monitor_cannot_trade():
    tree = ast.parse(SRC.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.risk.engine",
                                 "bot.risk.modes", "bot.paper", "bot.forward", "bot.strategies")) for m in mods), mods
    assert ".env" not in SRC.read_text().replace("never reads `.env`", "")


@pytest.mark.parametrize("value,level", [("0.98", None), ("0.96", "warning"), ("0.935", "critical")])
def test_simulated_accounts_are_checked_for_drawdown_from_their_saved_index(home, value, level):
    """The paper and forward runners write no drawdown in their status line: the saved index gives it."""
    fwd = home / "forward-h2b"
    state = json.loads((fwd / "state.json").read_text())
    state["index"] = {"units": "10000", "value_per_unit": value, "peak_value_per_unit": "1"}
    (fwd / "state.json").write_text(json.dumps(state))
    (fwd / "journal.jsonl").write_text(json.dumps({"at": NOW - 60, "event": "status", "equity": "10001",
                                                   "mode": "RUNNING"}) + "\n")       # as the paper runner writes it
    got = [f.level for f in acct(home) if f.rule == "drawdown" and f.subject == "forward"]
    assert got == ([level] if level else [])
