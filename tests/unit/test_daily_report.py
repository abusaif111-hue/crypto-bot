"""The daily report (docs/intel-plan.md, section 6): fixed conclusion rules, saved whole, sent once a day."""
import argparse
import ast
import hashlib
import io
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli, daily_report as dr
from bot.alerts import TelegramError
from bot.dashboard import Account
from bot.intel.market import Observation
from bot.monitor import Finding
from bot.signal_report import CoinSignal, Lookback
from tests.unit.test_monitor import CFG, NOW, home  # noqa: F401  (the fixture)

SRC = Path(__file__).resolve().parents[2] / "src" / "bot" / "daily_report.py"
FWD = {"started_at": NOW - 10 * 86400, "symbols": ["BTCUSDT"]}


def acct(name="forward", money="simulated", **kw):
    base = dict(found=True, mode="RUNNING", equity=Dec("10001"), started_with=Dec("10000"), drawdown_pct=Dec("0.10"))
    return Account(name, money, **{**base, **kw})


def obs(symbol="BTCUSDT", **kw):
    base = dict(day="2026-10-23", close=Dec("81189.43"), change_1d_pct=Dec("-0.28"), change_7d_pct=Dec("-0.52"),
                above_avg50=False, from_avg50_pct=Dec("-0.77"), avg50_rising=True, regime="bull",
                vol30_pct=Dec("1.29"), vol_rank_pct=Dec("1.9"), volume_vs_avg=Dec("0.81"), spread_pct=Dec("0"))
    return Observation(symbol, **{**base, **kw})


def weak(symbol="BTCUSDT"):
    lbs = [Lookback(14, Dec("-0.01"), Dec("0.03"), False), Lookback(28, Dec("0.02"), Dec("0.05"), False),
           Lookback(56, Dec("0.08"), Dec("0.07"), True)]
    return CoinSignal(symbol, False, "1 of 3 lookbacks strong", lookbacks=lbs, paused=False)


def crit(rule="stale prices", subject="BTCUSDT"):
    return Finding("critical", rule, subject, "the newest finished candle is too old")


def build(findings=(), accounts=None, signals=None, forward=FWD, market=True, observations=None):
    return dr.build(NOW, [acct()] if accounts is None else accounts, list(findings),
                    [obs()] if observations is None else observations,
                    [weak()] if signals is None else signals, forward, market)


# ---------------------------------------------------------------- the conclusion
def test_a_running_forward_test_without_a_breach_means_further_simulation_only():
    r = build()
    assert r.conclusion == dr.SUFFICIENT and "far too early" in r.reason


def test_any_critical_finding_pauses_the_analysis():
    r = build([crit(), Finding("warning", "drawdown", "live", "x")])
    assert r.conclusion == dr.PAUSE and "stale prices (BTCUSDT)" in r.reason


def test_without_a_forward_test_the_evidence_is_insufficient():
    assert build(forward={}).conclusion == dr.INSUFFICIENT
    assert build(accounts=[acct("live", "REAL money")]).conclusion == dr.INSUFFICIENT


def test_a_tripped_forward_breaker_is_insufficient_evidence():
    r = build(accounts=[acct(breaches=["REJ_DRAWDOWN_BREAKER"])])
    assert r.conclusion == dr.INSUFFICIENT and "breaker" in r.reason


# ---------------------------------------------------------------- the sections
def titles(r):
    return [t for t, _ in r.sections]


def section(r, title):
    return dict(r.sections)[title]


def test_the_report_has_every_section_the_plan_asks_for_in_order():
    r = build()
    assert titles(r) == ["Market regime and uncertainty", "News", "Major-asset observations",
                         "Scenarios and invalidation (H2b's rules)", "Risk", "Research evidence",
                         "Data quality and API health"]
    text = r.text()
    assert text.startswith(f"Daily report, {r.date}") and "Not a forecast, not advice, no profit target." in text
    assert "Conclusion: " + dr.SUFFICIENT in text and "gap through a stop" in text


def test_market_lines_come_from_the_observations():
    r = build(observations=[obs(), obs("ETHUSDT", above_avg50=True, from_avg50_pct=Dec("2.10")),
                            Observation("XRPUSDT", error="ConnectionError: no route")])
    lines = section(r, "Market regime and uncertainty")
    assert lines[0] == "Breadth: 1 of 2 coins above their 50-day average."
    assert "BTCUSDT: regime bull; below its 50-day average (-0.77%); volatility 1.29% a day, higher than 1.9% of its year." in lines
    assert "XRPUSDT: could not read prices (ConnectionError: no route)." in lines
    assert lines[-1].startswith("Uncertainty: no coin shows abnormal volatility")
    detail = section(r, "Major-asset observations")
    assert detail[0] == "BTCUSDT 2026-10-23: close 81189.43; day -0.28%, week -0.52%; volume 0.81x its 30-day average."


def test_unusual_volatility_is_the_uncertainty_line():
    f = Finding("warning", "abnormal volatility", "ETHUSDT", "30-day volatility is in the top 10% of its past year")
    assert section(build([f]), "Market regime and uncertainty")[-1] == \
        "Uncertainty: ETHUSDT 30-day volatility is in the top 10% of its past year"


def test_news_says_it_is_not_covered_rather_than_inventing_any():
    assert "Not covered yet" in section(build(), "News")[0]


def test_scenarios_are_h2bs_own_bars_and_open_trades_show_their_exits():
    live = acct("live", "REAL money", open_trades=[{"symbol": "BTCUSDT", "stop": "74000", "target": "98000"}])
    lines = section(build(accounts=[acct(), live], signals=[weak(), CoinSignal("ETHUSDT", True, "")]),
                    "Scenarios and invalidation (H2b's rules)")
    assert lines[0] == ("BTCUSDT: no buy: 1 of 3 returns strong, H2b needs 2. 14d -1.0% (needs above +3.0%), "
                        "28d +2.0% (needs above +5.0%), 56d +8.0% (needs above +7.0%).")
    assert lines[1].startswith("ETHUSDT: H2b's buy conditions are met; the risk engine still decides")
    assert "live holds BTCUSDT: stop 74000, target 98000" in lines[2]
    assert lines[-1] == "These are H2b's own rules applied to today's prices, not predictions."
    assert "could not be worked out" in section(dr.build(NOW, [acct()], [], [obs()], None, FWD), "Scenarios and invalidation (H2b's rules)")[0]


def test_risk_lists_every_account_and_every_warning():
    live = acct("live", "REAL money", equity=Dec("100"), started_with=Dec("100"), drawdown_pct=Dec("0.00"))
    lines = section(build([Finding("warning", "drawdown", "forward", "drawdown is past half the breaker")],
                          accounts=[live, acct(), Account("paper", "simulated")]), "Risk")
    assert lines[0] == "live (REAL money): 100.00 USDT of 100.00 at the start; drawdown 0.00% (breaker 6%); RUNNING; 0 open, 0 closed."
    assert len([x for x in lines if "(" in x and "USDT" in x]) == 2                 # paper is not set up: no line
    assert lines[-1] == "WARNING: forward drawdown: drawdown is past half the breaker."
    assert section(build(), "Risk")[-1] == "No risk warnings."


def test_evidence_is_the_research_record_and_the_forward_tests_progress():
    lines = section(build(accounts=[acct(closed=3)]), "Research evidence")
    assert any(x.startswith("H4 hourly pullbacks: walk-forward 331 trades, -1.51 USDT per trade") for x in lines)
    assert any("H2b blended momentum" in x and "+1.90 USDT per trade with doubled costs" in x for x in lines)
    assert lines[-1] == ("H2b forward test: day 10 of at least 365; 3 closed trades of at least 30. "
                         "Simulated results so far are not evidence yet.")


def test_data_health_shows_problems_or_says_all_is_fresh():
    assert section(build(), "Data quality and API health")[0].startswith("Prices fresh")
    lines = section(build([crit()]), "Data quality and API health")
    assert lines == ["CRITICAL: BTCUSDT stale prices: the newest finished candle is too old."]
    assert "Prices were not checked in this run." in section(build(market=False), "Data quality and API health")
    note = Finding("info", "insufficient evidence", "XRPUSDT", "not enough daily history to judge volatility")
    assert section(build([note]), "Data quality and API health")[-1] == \
        "Note: XRPUSDT: not enough daily history to judge volatility."


def test_the_telegram_version_is_short_and_points_to_the_full_report():
    short = build().short()
    assert short[0].startswith("📋 Daily report") and short[1].startswith("Conclusion: ")
    assert "Risk:" in short and "Market regime and uncertainty:" in short and "News:" not in short
    assert short[-1].endswith("(and the dashboard).")


# ---------------------------------------------------------------- saving and sending
class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise TelegramError("network: URLError")
        self.sent.append(text)


def test_reports_are_saved_whole_and_a_rerun_replaces_the_day(tmp_path):
    r = build()
    p = dr.save(r, tmp_path)
    assert p.read_text() == r.text() == (tmp_path / "latest.md").read_text()
    assert json.loads((tmp_path / f"{r.date}.json").read_text())["conclusion"] == dr.SUFFICIENT
    dr.save(build([crit()]), tmp_path)
    assert "Pause analysis" in p.read_text() and not list(tmp_path.glob("*.tmp"))


def test_the_message_goes_once_a_day(tmp_path):
    r, t = build(), FakeTelegram()
    assert dr.send_once(r, tmp_path, t) == "sent" and len(t.sent) == 1
    assert dr.send_once(r, tmp_path, t) == "already sent today" and len(t.sent) == 1
    assert dr.send_once(r, tmp_path, t, resend=True) == "sent" and len(t.sent) == 2


def test_a_run_that_stopped_while_sending_is_never_repeated_on_its_own(tmp_path):
    r, t = build(), FakeTelegram()
    (tmp_path / dr.SENT_FILE).write_text(json.dumps({r.date: "sending"}))
    assert "may or may not have arrived" in dr.send_once(r, tmp_path, t) and t.sent == []


def test_the_day_is_marked_sending_before_the_message_goes(tmp_path):
    r, seen = build(), []

    class Peek:
        def send(self, text):
            seen.append(json.loads((tmp_path / dr.SENT_FILE).read_text())[r.date])
    dr.send_once(r, tmp_path, Peek())
    assert seen == ["sending"] and json.loads((tmp_path / dr.SENT_FILE).read_text())[r.date] == "sent"


def test_a_failed_send_is_tried_again_next_run(tmp_path):
    r = build()
    with pytest.raises(TelegramError):
        dr.send_once(r, tmp_path, FakeTelegram(fail=True))
    assert json.loads((tmp_path / dr.SENT_FILE).read_text()) == {r.date: "failed"}
    t = FakeTelegram()
    assert dr.send_once(r, tmp_path, t) == "sent" and len(t.sent) == 1


# ---------------------------------------------------------------- the command
def _hashes(folder):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def args_for(folder, **kw):
    return argparse.Namespace(config="x", home=str(folder), reports_dir=str(folder / "reports"),
                              monitor_dir=str(folder / "monitor"), no_market=True, send=False, resend=False, **kw)


def test_the_command_reads_only_and_saves_in_its_own_folders(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli, "load_config", lambda p: CFG)
    before = _hashes(home)
    out = io.StringIO()
    assert cli._report(args_for(home), out=out) == 0
    after = _hashes(home)
    assert {k: v for k, v in after.items() if "/reports/" not in k and "/monitor/" not in k} == before
    assert "Conclusion: " in out.getvalue() and "Prices were not checked" in out.getvalue()
    assert any(k.endswith("reports/latest.md") for k in after) and any(k.endswith("monitor/findings.jsonl") for k in after)


def test_the_command_sends_once_and_reports_a_failure_clearly(home, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "load_config", lambda p: CFG)
    t = FakeTelegram()
    creds = lambda: argparse.Namespace(token="1:x", chat_id="5")  # noqa: E731
    a = args_for(home)
    a.send = True
    out = io.StringIO()
    assert cli._report(a, load=creds, telegram=lambda token, chat: t, out=out) == 0
    assert cli._report(a, load=creds, telegram=lambda token, chat: t, out=out) == 0
    assert len(t.sent) == 1 and "Telegram: already sent today" in out.getvalue()
    a.resend = True
    assert cli._report(a, load=creds, telegram=lambda token, chat: FakeTelegram(fail=True), out=io.StringIO()) == 2
    assert "the report is saved but was not sent" in capsys.readouterr().err


def test_a_binance_connection_failure_pauses_the_analysis(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli, "load_config", lambda p: CFG)

    def broken(environment):
        raise ConnectionError("dns failed")
    a = args_for(home)
    a.no_market = False
    out = io.StringIO()
    assert cli._report(a, make_exchange=broken, out=out) == 0
    assert "Conclusion: Pause analysis" in out.getvalue() and "data outage (binance)" in out.getvalue()


def test_the_candle_cache_fetches_each_coin_once():
    calls = []

    class Data:
        venue = object()

        def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
            calls.append((symbol, limit))
            return list(range(limit))
    c = cli._Candles(Data())
    assert c.closed_candles("BTCUSDT", "4h", limit=2394)[-1] == 2393
    assert c.closed_candles("BTCUSDT", "4h", limit=10) == list(range(2384, 2394))
    assert c.closed_candles("BTCUSDT", "4h", limit=3000)[-1] == 2999 and calls == [("BTCUSDT", 2394), ("BTCUSDT", 3000)]


def test_the_command_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_report", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["report", "--send"])
    assert seen["args"].send and not seen["args"].resend and seen["args"].reports_dir == "~/reports"


def test_the_report_cannot_trade_and_never_reads_keys():
    tree = ast.parse(SRC.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("bot.execution", "bot.exchange", "bot.risk.engine", "bot.risk.modes", "bot.paper",
                                 "bot.backtest", "bot.forward")) for m in mods), mods
    assert "credentials" not in SRC.read_text() and "load_telegram" not in SRC.read_text()


def test_the_dashboard_shows_the_latest_report(tmp_path):
    from bot import dashboard
    assert "No report yet" in dashboard.render([], NOW, dashboard.read_report(tmp_path))
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "latest.md").write_text("Daily report, x\nConclusion: <b>Insufficient</b>\nrest\n")
    page = dashboard.render([], NOW, dashboard.read_report(tmp_path))
    assert "Conclusion: &lt;b&gt;Insufficient&lt;/b&gt;" in page and 'href="/report"' in page and "rest" not in page
