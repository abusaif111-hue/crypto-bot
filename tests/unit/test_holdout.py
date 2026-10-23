"""Step 3: the one-time holdout test. Locked without a ticket, recorded, VOID on a second run,
and nothing can use it up by mistake."""
import argparse
import io
import json
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.backtest import history, holdout, walkforward
from bot.backtest.engine import run
from bot.backtest.periods import HOLDOUT_END_MS, HOLDOUT_START_MS, HoldoutLocked, HoldoutTicket
from bot.core.config import AppConfig
from tests.backtest_data import H4, rules, walk

PHRASE = holdout.CONFIRM_PHRASE


def data_through_holdout(n=8200):
    start = HOLDOUT_END_MS - n * H4
    return {"BTCUSDT": walk(start, n, "20000", 31, vol=0.015), "ETHUSDT": walk(start, n, "1500", 32, vol=0.015)}


def save(tmp_path, candles):
    for s, cs in candles.items():
        history.write_csv(tmp_path / f"{s}-4h.csv", cs)
        (tmp_path / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(rules((s,))[s])))


# ---------------------------------------------------------------- the lock
def test_a_ticket_cannot_be_made_outside_the_holdout_command():
    with pytest.raises(HoldoutLocked):
        HoldoutTicket("someone", "now", [], object())


def test_open_needs_the_exact_phrase_and_a_name(tmp_path):
    with pytest.raises(HoldoutLocked):
        holdout.open_holdout(tmp_path, "H2", "Sultan", "yes")
    with pytest.raises(HoldoutLocked):
        holdout.open_holdout(tmp_path, "H2", " ", PHRASE)
    assert holdout.read_log(tmp_path) == []                     # nothing recorded, nothing opened


def test_every_opening_is_recorded_and_a_second_is_void(tmp_path):
    t1 = holdout.open_holdout(tmp_path, "H2", "Sultan", PHRASE)
    assert not t1.void
    holdout.finish(tmp_path, "H2", t1, "FAILS", "/r")
    t2 = holdout.open_holdout(tmp_path, "H2", "Sultan", PHRASE)
    assert t2.void and t2.previous_runs == [t1.opened_at]
    log = holdout.read_log(tmp_path)
    assert [r["status"] for r in log] == ["completed", "started"] and log[1]["void"] is True
    assert not holdout.open_holdout(tmp_path, "H1", "Sultan", PHRASE).void   # per strategy


def test_holdout_candles_need_a_ticket(tmp_path):
    cs = data_through_holdout(400)
    save(tmp_path, cs)
    with pytest.raises(HoldoutLocked):
        history.load_through_holdout(tmp_path, "BTCUSDT", "4h", None)
    assert all(c.close_time < HOLDOUT_START_MS for c in history.load(tmp_path, "BTCUSDT"))
    t = holdout.open_holdout(tmp_path, "H2", "Sultan", PHRASE)
    got = history.load_through_holdout(tmp_path, "BTCUSDT", "4h", t)
    assert got[-1].close_time >= HOLDOUT_START_MS and all(c.close_time < HOLDOUT_END_MS for c in got)
    assert history.covers(tmp_path, "BTCUSDT", "4h", HOLDOUT_END_MS)


def test_engine_replays_the_holdout_only_with_a_ticket(tmp_path):
    from bot.strategies.random_entry import RandomEntry
    cs = data_through_holdout(600)
    with pytest.raises(HoldoutLocked):
        run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=1), cs, rules(), AppConfig(), start_ms=HOLDOUT_START_MS)
    t = holdout.open_holdout(tmp_path, "H2", "Sultan", PHRASE)
    res = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=1), cs, rules(), AppConfig(), start_ms=HOLDOUT_START_MS,
              end_ms=HOLDOUT_END_MS, holdout=t)
    assert res.equity and res.equity[-1].at_ms <= HOLDOUT_END_MS
    later = {k: v + walk(HOLDOUT_END_MS, 3, "100", 1) for k, v in cs.items()}
    with pytest.raises(HoldoutLocked):
        run(RandomEntry(["BTCUSDT"], seed=1), later, rules(), AppConfig(), holdout=t)


# ---------------------------------------------------------------- walk-forward report lookup
def write_wf(reports, name="20261023-081325-strategy-H2", passes=True, sha="abc", universe=None):
    f = reports / name
    f.mkdir(parents=True)
    (f / "results.json").write_text(json.dumps({
        "meta": {"config_sha256": sha, "universe": universe}, "passes_walk_forward": passes,
        "oos": {"trades": 121, "pnl": "327.55", "by_coin": {"BTCUSDT": "200", "ETHUSDT": "127.55"},
                "by_month": {"2024-02": "134.48", "2024-03": "193.07"}}}))
    return f


def test_find_walk_forward_needs_a_matching_pass(tmp_path):
    write_wf(tmp_path, "20261023-060000-strategy-H2", passes=False)
    with pytest.raises(HoldoutLocked):
        holdout.find_walk_forward(tmp_path, "H2", "abc", None)
    good = write_wf(tmp_path, "20261023-081325-strategy-H2")
    assert holdout.find_walk_forward(tmp_path, "H2", "abc", None)[0] == good
    with pytest.raises(HoldoutLocked):
        holdout.find_walk_forward(tmp_path, "H2", "other-config", None)
    with pytest.raises(HoldoutLocked):
        holdout.find_walk_forward(tmp_path, "H2", "abc", "BTCUSDT (majors)")


# ---------------------------------------------------------------- the command
def hargs(tmp_path, confirm=PHRASE, **kw):
    a = dict(config="config/default.yaml", data_dir=str(tmp_path / "data"), interval="4h", strategy="H2",
             operator="Sultan", confirm=confirm, screened=False, reports_dir=str(tmp_path / "rep"))
    a.update(kw)
    return argparse.Namespace(**a)


def prepared(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    save(tmp_path / "data", data_through_holdout())
    from bot.backtest import report, universe
    research = {s: history.load(tmp_path / "data", s) for s in ("BTCUSDT", "ETHUSDT")}
    from bot.core.config import load_config
    sha = report.provenance(load_config("config/default.yaml"), research, report.CostModel())["config_sha256"]
    write_wf(tmp_path / "rep", sha=sha)
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)


@pytest.mark.parametrize("why", ["phrase", "no data", "no walk-forward pass"])
def test_mistakes_never_open_the_holdout(tmp_path, monkeypatch, capsys, why):
    prepared(tmp_path, monkeypatch)
    if why == "no data":
        (tmp_path / "data" / "ETHUSDT-4h.csv").write_text(
            (tmp_path / "data" / "ETHUSDT-4h.csv").read_text().splitlines()[0] + "\n")
    if why == "no walk-forward pass":
        import shutil
        shutil.rmtree(tmp_path / "rep")
    args = hargs(tmp_path, confirm="yes" if why == "phrase" else PHRASE)
    assert cli._backtest_holdout(args, out=io.StringIO()) == 2
    assert "The holdout was not opened" in capsys.readouterr().err
    assert holdout.read_log(tmp_path / "data") == []


def test_the_holdout_runs_once_and_a_second_run_is_void(tmp_path, monkeypatch):
    prepared(tmp_path, monkeypatch)
    out = io.StringIO()
    assert cli._backtest_holdout(hargs(tmp_path), out=out) == 0
    text = out.getvalue()
    for must in ("Holdout opened by Sultan", "# H2 holdout test", "Verdict:", "Holdout: positive expectancy after "
                 "doubled costs", "Walk-forward + holdout: at least 100 trades", "Settings chosen on the 24 months"):
        assert must in text, must
    log = holdout.read_log(tmp_path / "data")
    assert len(log) == 1 and log[0]["status"] == "completed" and log[0]["verdict"]
    saved = json.loads(next((tmp_path / "rep").glob("*-holdout-H2/results.json")).read_text())
    assert saved["void"] is False and len(saved["checks"]) == 5
    out2 = io.StringIO()
    assert cli._backtest_holdout(hargs(tmp_path), out=out2) == 0
    assert "VOID" in out2.getvalue() and "cannot qualify" in out2.getvalue()
    assert holdout.read_log(tmp_path / "data")[1]["void"] is True


def test_checks_combine_walk_forward_and_holdout(tmp_path):
    from bot.backtest.metrics import Metrics

    def m(trades, expectancy, dd="0.01"):
        return Metrics(Dec(10000), Dec(10100), Dec("0.01"), None, Dec(0), None, Dec(dd), Dec(0), Dec(365), Dec(0),
                       trades=trades, expectancy=Dec(expectancy))

    class Res:
        drawdown_halt_ms = None
        trades: list = []
    ev = holdout.HoldoutEvaluation(
        name="H2", params={}, train_score=None, deployed=Res(), edge=Res(), edge_doubled=Res(),
        metrics={"deployed": m(30, "2"), "edge": m(30, "2"), "edge_doubled": m(30, "0.5")}, wf_folder="/r",
        wf_trades=121, combined_trades=151, combined_pnl=Dec(400),
        by_coin={"BTCUSDT": Dec(300), "ETHUSDT": Dec(100)}, by_month={"2024-02": Dec(150), "2026-03": Dec(250)},
        void=False, previous_runs=[])
    ev.checks = holdout.checks(ev, AppConfig())
    assert ev.passes and "121 + 30 = 151" in ev.checks[2].detail
    ev.metrics["edge_doubled"] = m(30, "-0.1")
    ev.checks = holdout.checks(ev, AppConfig())
    assert not ev.passes and not ev.checks[0].passed
    ev.metrics["edge_doubled"] = m(30, "0.5")
    ev.metrics["deployed"] = m(30, "2", dd="0.061")
    ev.checks = holdout.checks(ev, AppConfig())
    assert not ev.checks[1].passed
    ev.metrics["deployed"] = m(30, "2")
    ev.checks, ev.void, ev.previous_runs = holdout.checks(ev, AppConfig()), True, ["earlier"]
    assert all(c.passed for c in ev.checks) and not ev.passes and holdout.verdict(ev).startswith("VOID")
