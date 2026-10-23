"""The H2b forward test's end-of-test report: the plan's 6 checks, judged once, replays only the forward months."""
import argparse
import io
import json
import time
from decimal import Decimal as Dec

import pytest

from bot import cli, forward, forward_report
from bot.backtest import history, universe
from bot.backtest.engine import run
from bot.backtest.periods import HOLDOUT_END_MS, HOLDOUT_START_MS, HoldoutLocked
from bot.core.config import AppConfig
from bot.paper import store
from bot.paper.runner import JOURNAL_FILE, STATE_FILE
from bot.paper.sim import ClosedTrade, Fill, SimExchange
from bot.risk.models import RejectCode
from bot.strategies.random_entry import RandomEntry
from tests.backtest_data import H4, rules, walk
from tests.unit.test_forward import COINS, T0, FakeChoose, started

DAY = 86_400
CFG = AppConfig().model_validate({**AppConfig().model_dump(), "universe": COINS})


@pytest.fixture
def fake_choose(monkeypatch):
    f = FakeChoose()
    monkeypatch.setattr(forward, "choose", f)
    return f


def trade(symbol="BTCUSDT", pnl="10", day=1, fee="1", strategy="H2b"):
    opened, closed = T0 + day * DAY, T0 + day * DAY + 3600
    return ClosedTrade(symbol, strategy, Dec(1), Dec(100), Dec(110), "STRATEGY_EXIT", Dec(fee), Dec(fee), Dec(pnl),
                       Dec(5), False, opened, closed)


def fills_for(trades, slip="0.5"):
    out = []
    for t in trades:
        out.append(Fill("b", t.symbol, "BUY", "ENTRY", t.qty, t.entry_price, t.entry_fee, t.opened_at, Dec(slip)))
        out.append(Fill("s", t.symbol, "SELL", "STRATEGY_EXIT", t.qty, t.exit_price, t.exit_fee, t.closed_at, Dec(slip)))
    return out


def evaluation(trades, breaker=None, random=(Dec("1"),) * 20, slip="0.5"):
    by_coin, by_month = {}, {}
    for t in trades:
        by_coin[t.symbol] = by_coin.get(t.symbol, Dec(0)) + t.pnl
        m = time.strftime("%Y-%m", time.gmtime(t.closed_at))
        by_month[m] = by_month.get(m, Dec(0)) + t.pnl
    f = fills_for(trades, slip)
    ev = forward_report.ForwardEvaluation(
        trades=trades, start_ms=int(T0 * 1000), end_ms=int(T0 * 1000) + 400 * DAY * 1000,
        expectancy=sum((t.pnl for t in trades), Dec(0)) / len(trades) if trades else None,
        doubled_expectancy=(sum((t.pnl - forward_report.extra_costs(t, f) for t in trades), Dec(0)) / len(trades)
                            if trades else None),
        breaker_tripped_at=breaker, by_coin=by_coin, by_month=by_month, random_expectancies=list(random),
        btc_buy_and_hold=None, replay_trades=0, replay_expectancy=None)
    ev.checks = forward_report.checks(ev, AppConfig())
    return ev


def spread(n=30, pnl="10"):
    """n trades over 4 coins and 10 months: nothing drives the result."""
    return [trade(COINS[i % 4], pnl, day=1 + 10 * i) for i in range(n)]


# ---------------------------------------------------------------- the six checks
def test_all_six_pass_on_a_broad_profitable_record():
    ev = evaluation(spread())
    assert [c.passed for c in ev.checks] == [True] * 6 and ev.passes
    assert forward_report.verdict(ev).startswith("PASSES")


def test_doubled_costs_take_one_more_round_of_fees_and_slippage():
    t = trade(pnl="2.9", fee="1")                       # 2.9 - (1 + 1 + 0.5 + 0.5) = -0.1
    assert forward_report.extra_costs(t, fills_for([t])) == Dec("3.0")
    ev = evaluation(spread(pnl="2.9"))
    assert not ev.checks[0].passed and ev.checks[0].detail.startswith("-0.10 USDT per trade")
    with pytest.raises(forward_report.ReportError):
        forward_report.extra_costs(t, [])                 # records incomplete: never guessed


@pytest.mark.parametrize("case, failing", [
    ("breaker", 1), ("29 trades", 2), ("one coin", 3), ("one month", 4), ("random", 5)])
def test_each_check_fails_on_its_own(case, failing):
    trades = spread()
    kw = {}
    if case == "breaker":
        kw["breaker"] = T0 + 40 * DAY
    if case == "29 trades":
        trades = spread(29)
    if case == "one coin":
        trades = [trade(COINS[i % 4], "100" if i % 4 == 0 else "-5", day=1 + 10 * i) for i in range(30)]
    if case == "one month":
        trades = [trade(COINS[i % 4], "100" if i < 3 else "-2", day=1 + i if i < 3 else 31 + 9 * i)
                  for i in range(30)]
    if case == "random":
        kw["random"] = [Dec(1)] * 18 + [Dec(50)] * 2    # median 1 < 10 per trade, 95th percentile 50 > 10
    ev = evaluation(trades, **kw)
    assert [i for i, c in enumerate(ev.checks) if not c.passed] == [failing], [c.detail for c in ev.checks]
    assert forward_report.verdict(ev).startswith("FAILS")


def test_no_trades_fails_and_says_why():
    ev = evaluation([], random=())
    assert not ev.passes and {c.detail for c in ev.checks[3:]} == {"no trades", "not enough trades"}


# ---------------------------------------------------------------- the replay: forward months only
def forward_candles(days_after=30):
    start = int(T0 * 1000) - 2600 * H4                  # warm-up reaches back into the spent holdout year
    n = 2600 + days_after * 6
    return {s: walk(start, n, "100", i + 1, vol=0.02) for i, s in enumerate(COINS)}


def test_the_forward_replay_trades_only_from_the_forward_start():
    cs = forward_candles()
    start = int(T0 * 1000)
    assert min(c.close_time for c in cs["BTCUSDT"]) < HOLDOUT_END_MS          # holdout-year history is present
    r = run(RandomEntry(COINS, seed=1, chance_per_candle=0.3), cs, rules(COINS), CFG, start_ms=start,
            edge=True, forward_from=start)
    assert r.trades and all(t.opened_at * 1000 >= start for t in r.trades)
    with pytest.raises(HoldoutLocked):                                        # trading in the holdout year: never
        run(RandomEntry(COINS, seed=1), cs, rules(COINS), CFG, start_ms=HOLDOUT_START_MS, forward_from=HOLDOUT_START_MS)
    with pytest.raises(HoldoutLocked):                                        # starting before the forward start
        run(RandomEntry(COINS, seed=1), cs, rules(COINS), CFG, start_ms=start - H4, forward_from=start)
    with pytest.raises(HoldoutLocked):                                        # without the option: locked as before
        run(RandomEntry(COINS, seed=1), cs, rules(COINS), CFG, start_ms=start)


def test_evaluate_counts_only_trades_closed_by_the_end(fake_choose, tmp_path):
    ft = started(tmp_path, fake_choose)
    end_ms = int(T0 * 1000) + 30 * DAY * 1000
    late = trade(day=31)
    trades = spread(10)[:4] + [late]
    trades = [t if t is late else trade(t.symbol, "10", day=2) for t in trades]
    prog = forward.progress(ft, [], None, len(trades), T0 + 30 * DAY)
    ev = forward_report.evaluate(ft, trades, fills_for(trades), prog, forward_candles(), rules(COINS), CFG, end_ms,
                                 random_runs=2)
    assert late not in ev.trades and len(ev.trades) == 4
    assert ev.expectancy == Dec(10) and ev.doubled_expectancy == Dec(7)        # 10 - (1 + 1 + 0.5 + 0.5)
    assert ev.start_ms == int(T0 * 1000) and ev.end_ms == end_ms
    assert ev.btc_buy_and_hold is not None and len(ev.checks) == 6


# ---------------------------------------------------------------- the command: once, after the end
class Venue:
    def __init__(self):
        self.fetched = []

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.fetched.append(symbol)
        return []

    def symbol_rules(self, symbols):
        return {s: rules((s,))[s] for s in symbols}


def rargs(tmp_path, **kw):
    a = dict(config="config/default.yaml", state_dir=str(tmp_path / "fw"), data_dir=str(tmp_path / "data"),
             reports_dir=str(tmp_path / "rep"), operator="Sultan", random_runs=2)
    a.update(kw)
    return argparse.Namespace(**a)


def judged_setup(tmp_path, fake_choose, monkeypatch, breaker=True, trades=None):
    fw = tmp_path / "fw"
    started(fw, fake_choose)
    (tmp_path / "data").mkdir()
    for s, cs in forward_candles().items():
        history.write_csv(tmp_path / "data" / f"{s}-4h.csv", cs)
        (tmp_path / "data" / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(rules((s,))[s])))
    sim = SimExchange(CFG, None, clock=lambda: T0)
    sim.trades = trades if trades is not None else [trade(COINS[i % 4], "10", day=2 + i) for i in range(8)]
    sim.fills = fills_for(sim.trades)
    store.save(sim, fw / STATE_FILE, T0 + 30 * DAY)
    if breaker:
        (fw / JOURNAL_FILE).write_text(json.dumps({"at": T0 + 25 * DAY, "event": "breaker",
                                                   "breach": RejectCode.DRAWDOWN.value}) + "\n")
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)
    monkeypatch.setattr(time, "time", lambda: T0 + 30 * DAY)


def test_the_report_is_refused_while_the_test_runs(tmp_path, fake_choose, monkeypatch, capsys):
    judged_setup(tmp_path, fake_choose, monkeypatch, breaker=False)
    assert cli._forward_report(rargs(tmp_path), make_exchange=lambda **k: Venue(), out=io.StringIO()) == 2
    err = capsys.readouterr().err
    assert "has not ended" in err and "Nothing was judged" in err
    assert forward_report.read_verdict(tmp_path / "fw") is None


def test_judged_once_after_the_breaker_tripped(tmp_path, fake_choose, monkeypatch):
    judged_setup(tmp_path, fake_choose, monkeypatch)
    venue = Venue()
    out = io.StringIO()
    assert cli._forward_report(rargs(tmp_path), make_exchange=lambda **k: venue, out=out) == 0
    text = out.getvalue()
    assert "FAILS the forward test" in text and "| 2 | The 6% drawdown breaker never trips | FAIL |" in text
    assert "is final" in text and venue.fetched == []                      # the cache already reached the end
    saved = forward_report.read_verdict(tmp_path / "fw")
    assert saved["operator"] == "Sultan" and saved["passes"] is False
    venue2, out2 = Venue(), io.StringIO()
    assert cli._forward_report(rargs(tmp_path, operator="someone"), make_exchange=lambda **k: venue2, out=out2) == 0
    assert "Already judged" in out2.getvalue() and venue2.fetched == []      # never judged again
    assert forward_report.read_verdict(tmp_path / "fw") == saved


def test_the_report_needs_the_runner_stopped(tmp_path, fake_choose, monkeypatch, capsys):
    judged_setup(tmp_path, fake_choose, monkeypatch)
    monkeypatch.setattr(cli, "_runner_active", lambda d: True)
    assert cli._forward_report(rargs(tmp_path), make_exchange=lambda **k: Venue(), out=io.StringIO()) == 2
    assert "systemctl stop bot-forward" in capsys.readouterr().err


def test_nothing_is_judged_on_incomplete_prices(tmp_path, fake_choose, monkeypatch, capsys):
    judged_setup(tmp_path, fake_choose, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: T0 + 40 * DAY)              # prices end on day 30
    venue = Venue()
    assert cli._forward_report(rargs(tmp_path), make_exchange=lambda **k: venue, out=io.StringIO()) == 2
    assert sorted(venue.fetched) == sorted(COINS)                         # it asked Binance for the missing days
    assert "does not cover the forward test" in capsys.readouterr().err
    assert forward_report.read_verdict(tmp_path / "fw") is None
    assert len(history.load_with_forward(tmp_path / "data", "BTCUSDT")) == len(forward_candles()["BTCUSDT"])  # cache kept


def test_a_verdict_cannot_be_overwritten(tmp_path):
    forward_report.save_verdict(tmp_path, {"verdict": "first"})
    with pytest.raises(forward_report.ReportError):
        forward_report.save_verdict(tmp_path, {"verdict": "second"})
    assert forward_report.read_verdict(tmp_path) == {"verdict": "first"}
