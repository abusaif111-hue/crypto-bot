"""Module 3: the paper-trading runner and the `bot paper` commands. No network."""
import io
import json
from decimal import Decimal as Dec

import pytest

import bot.paper.sim as sim_mod
from bot.cli import main
from bot.core.config import AppConfig
from bot.paper.runner import JOURNAL_FILE, STATE_FILE, PaperRunner, RunnerLock, RunnerLocked, write_request
from bot.risk.models import Mode, TradeIntent
from tests.fakes import Clock, FakeMarket


def journal(state_dir):
    return [json.loads(line) for line in (state_dir / JOURNAL_FILE).read_text().splitlines()]


def events(state_dir):
    return [e["event"] for e in journal(state_dir)]


def opened(tmp_path, market=None, clock=None, **kw):
    market, clock = market or FakeMarket(), clock or Clock()
    return PaperRunner.open(tmp_path, AppConfig(), market, clock, **kw), market, clock


def trade_request(tmp_path, **over):
    req = dict(operator="sultan", reason="breakout above the range high", symbol="BTCUSDT", entry="80000",
               stop="78000", target="86000", hours=96, invalidation="daily close back below the range")
    req.update(over)
    write_request(tmp_path, "trade", **req)


def test_new_account_starts_blocked_and_is_saved(tmp_path):
    runner, _, _ = opened(tmp_path)
    assert runner.sim.modes.mode is Mode.ENTRIES_BLOCKED
    assert (tmp_path / STATE_FILE).exists()
    first = journal(tmp_path)[0]
    assert first["event"] == "started" and first["restored"] is False and first["cash"] == "10000"


def test_resume_then_manual_trade_goes_through_the_risk_engine(tmp_path, monkeypatch):
    calls = []
    real = sim_mod.evaluate
    monkeypatch.setattr(sim_mod, "evaluate", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    runner, market, clock = opened(tmp_path)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    trade_request(tmp_path)
    runner.tick()
    assert runner.sim.modes.mode is Mode.RUNNING
    assert [i.symbol for i in calls] == ["BTCUSDT"]
    assert "BTCUSDT" in runner.sim.positions
    ev = events(tmp_path)
    assert ev.index("mode_changed") < ev.index("entry_filled")      # journal is in the order things happened
    filled = [e for e in journal(tmp_path) if e["event"] == "entry_filled"][0]
    assert filled["source"] == "operator:sultan" and Dec(filled["price"]) == Dec("80040")
    assert not list((tmp_path / "requests").glob("*"))          # requests are consumed


def test_trade_request_while_blocked_is_rejected_and_logged(tmp_path):
    runner, _, _ = opened(tmp_path)
    trade_request(tmp_path)
    runner.tick()
    rej = [e for e in journal(tmp_path) if e["event"] == "entry_rejected"][0]
    assert rej["code"] == "REJ_MODE" and not runner.sim.positions


def test_exit_is_filled_and_journaled(tmp_path):
    runner, market, clock = opened(tmp_path)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    trade_request(tmp_path)
    runner.tick()
    clock.advance(5)
    market.set_bid("BTCUSDT", "86000")
    runner.tick()
    closed = [e for e in journal(tmp_path) if e["event"] == "trade_closed"][0]
    assert closed["exit_reason"] == "TAKE_PROFIT" and Dec(closed["pnl"]) > 0
    assert not runner.sim.positions


def test_market_data_outage_is_logged_once_and_recovery_noted(tmp_path):
    runner, market, clock = opened(tmp_path)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    trade_request(tmp_path)
    runner.tick()
    market.fail.add("books")
    for _ in range(3):
        clock.advance(5)
        runner.tick()
    market.fail.clear()
    market.set_bid("BTCUSDT", "77000")                 # price fell past the stop during the outage
    clock.advance(5)
    runner.tick()
    ev = events(tmp_path)
    assert ev.count("market_data_down") == 1 and ev.count("market_data_back") == 1
    closed = [e for e in journal(tmp_path) if e["event"] == "trade_closed"][0]
    assert closed["exit_reason"] == "STOP_LOSS" and closed["gapped"] is True


def test_restart_mid_trade_keeps_the_position_and_blocks_entries(tmp_path):
    runner, market, clock = opened(tmp_path)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    trade_request(tmp_path)
    runner.tick()
    cash = runner.sim.quote
    runner2, _, _ = opened(tmp_path, market, clock)
    assert runner2.sim.quote == cash and "BTCUSDT" in runner2.sim.positions
    assert runner2.sim.modes.mode is Mode.ENTRIES_BLOCKED
    assert [e for e in journal(tmp_path) if e["event"] == "started"][-1]["restored"] is True
    market.set_bid("BTCUSDT", "78000")
    runner2.tick()
    assert not runner2.sim.positions and runner2.sim.trades[-1].exit_reason == "STOP_LOSS"


def test_block_and_bad_requests(tmp_path):
    runner, _, _ = opened(tmp_path)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    runner.tick()
    write_request(tmp_path, "block", operator="sultan", reason="news event, pause entries")
    write_request(tmp_path, "launch", operator="x")
    (tmp_path / "requests" / "999-broken.json").write_text("{nope")
    runner.tick()
    assert runner.sim.modes.mode is Mode.ENTRIES_BLOCKED
    ev = events(tmp_path)
    assert "request_ignored" in ev and "request_failed" in ev
    modes = [e for e in journal(tmp_path) if e["event"] == "mode_changed"]
    assert [m["to_mode"] for m in modes] == ["RUNNING", "ENTRIES_BLOCKED"]


def test_resume_refused_while_a_breaker_is_active(tmp_path):
    runner, _, _ = opened(tmp_path)
    runner.sim.quote -= Dec("700")
    runner.tick()
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    runner.tick()
    assert runner.sim.modes.mode is Mode.ENTRIES_BLOCKED
    ev = events(tmp_path)
    assert "breaker" in ev and "resume_refused" in ev


class Proposer:
    name = "test"

    def __init__(self, intents=None, error=False):
        self.intents, self.error = intents or [], error

    def propose(self, data, now):
        if self.error:
            raise RuntimeError("bad strategy")
        out, self.intents = self.intents, []
        return out


def test_strategy_ideas_are_submitted_and_a_broken_strategy_does_not_stop_the_runner(tmp_path):
    intent = TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=Dec("80000"), stop_price=Dec("78000"),
                         take_profit_price=Dec("86000"), strategy="test", horizon_hours=96, invalidation="x")
    runner, _, _ = opened(tmp_path, strategies=[Proposer(error=True), Proposer([intent])])
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    runner.tick()
    ev = events(tmp_path)
    assert "strategy_error" in ev and "entry_filled" in ev and "BTCUSDT" in runner.sim.positions


def test_run_stops_cleanly_and_saves(tmp_path):
    runner, _, clock = opened(tmp_path)
    sleeps = []
    runner.run(interval_s=5, max_ticks=3, sleep=lambda s: (sleeps.append(s), clock.advance(s)))
    assert sleeps == [5, 5] and events(tmp_path)[-1] == "stopped"
    saved = json.loads((tmp_path / STATE_FILE).read_text())
    assert saved["saved_at"] == clock()


def test_second_runner_is_refused(tmp_path):
    import os

    with RunnerLock(tmp_path):
        with pytest.raises(RunnerLocked):
            with RunnerLock(tmp_path):
                pass
        assert (tmp_path / "runner.lock").read_text() == str(os.getpid())   # holder's PID kept
    with RunnerLock(tmp_path):                         # released after the first one exits
        pass


# ------------------------------------------------------------ CLI
def test_cli_paper_run_status_and_requests(tmp_path, capsys):
    from bot import cli

    market = FakeMarket()
    out = io.StringIO()
    assert cli._paper_run(cli_args("run", tmp_path, interval=0, ticks=1), make_exchange=lambda **k: market,
                          out=out) == 0
    assert "started" in out.getvalue()
    assert main(["paper", "resume", "--operator", "sultan", "--reason", "start paper trading",
                 "--state-dir", str(tmp_path)]) == 0
    assert main(["paper", "trade", "--operator", "sultan", "--reason", "breakout above the range",
                 "--entry", "80000", "--stop", "78000", "--target", "86000", "--state-dir", str(tmp_path)]) == 0
    assert "not running" in capsys.readouterr().out
    assert cli._paper_run(cli_args("run", tmp_path, interval=0, ticks=1), make_exchange=lambda **k: market,
                          out=io.StringIO()) == 0
    assert main(["paper", "status", "--state-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Mode: RUNNING" in text and "BTCUSDT" in text and "Runner: NOT running" in text
    assert "0 of 30 trades" in text and "simulated money only" in text


def test_cli_refuses_short_reasons_and_missing_account(tmp_path, capsys):
    assert main(["paper", "resume", "--operator", "sultan", "--reason", "go", "--state-dir", str(tmp_path)]) == 2
    assert main(["paper", "status", "--state-dir", str(tmp_path / "none")]) == 1


def test_cli_refuses_a_corrupt_account(tmp_path, capsys):
    (tmp_path / STATE_FILE).write_text("{broken")
    from bot import cli

    assert cli._paper_run(cli_args("run", tmp_path, interval=0, ticks=1), make_exchange=lambda **k: FakeMarket(),
                          out=io.StringIO()) == 2
    assert "cannot read the saved paper account" in capsys.readouterr().err
    assert (tmp_path / STATE_FILE).read_text() == "{broken"      # never overwritten with a fresh account


def test_cli_paper_needs_paper_environment(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("environment: testnet\n")
    from bot import cli

    args = cli_args("run", tmp_path, interval=0, ticks=1)
    args.config = str(cfg)
    assert cli._paper_run(args, make_exchange=lambda **k: FakeMarket(), out=io.StringIO()) == 2


def cli_args(cmd, state_dir, **kw):
    import argparse

    return argparse.Namespace(config="config/default.yaml", cmd="paper", paper_cmd=cmd, state_dir=str(state_dir), **kw)


def test_log_lines_show_short_numbers_journal_keeps_full_precision(tmp_path):
    out = io.StringIO()
    runner, _, _ = opened(tmp_path, out=out)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    trade_request(tmp_path)
    runner.tick()
    line = [x for x in out.getvalue().splitlines() if " entry_filled " in x][0]
    assert "price=80040.00" in line and "qty=0.00966" in line and "at 80040.00" in line
    assert "000000" not in line
    filled = [e for e in journal(tmp_path) if e["event"] == "entry_filled"][0]
    assert Dec(filled["fee"]) == Dec(filled["qty"]) * Dec("80040") * Dec("0.001")   # exact in the journal
