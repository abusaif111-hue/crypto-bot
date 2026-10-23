"""Module 4 step 2: simulator exits, edge mode, walk-forward windows and the selection rule."""
import argparse
import io
from datetime import datetime, timezone
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.backtest import selection, walkforward
from bot.backtest.engine import run
from bot.backtest.metrics import compute
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS
from bot.backtest.walkforward import Window, one_at_a_time, score, variants, windows
from bot.core.config import AppConfig
from bot.paper import fills
from bot.risk.models import RejectCode
from bot.strategies.base import Exit, RaiseStop, entry_limit, make_intent
from bot.strategies.h1_trend import TrendBreakout
from bot.strategies.h2_momentum import TimeSeriesMomentum
from bot.strategies.random_entry import RandomEntry
from tests.backtest_data import H4, rules, walk
from tests.unit.test_sim_exchange import INTENT, make


def ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- simulator exits
def test_raise_stop_only_tightens():
    sim, market, _ = make()
    assert sim.submit(INTENT).filled
    assert not sim.raise_stop("BTCUSDT", Dec("77000"))                # lower: refused
    assert not sim.raise_stop("BTCUSDT", Dec("80000"))                # at/above the bid: refused
    assert not sim.raise_stop("ETHUSDT", Dec("1"))                    # no position
    assert sim.raise_stop("BTCUSDT", Dec("79000"))
    assert sim.positions["BTCUSDT"].stop_price == Dec("79000")
    market.set_bid("BTCUSDT", "79000")
    (t,) = sim.poll()
    assert t.exit_reason == fills.STOP_LOSS and t.exit_price == Dec("79000") * Dec("0.995")


def test_close_position_sells_at_market():
    sim, market, _ = make()
    assert sim.submit(INTENT).filled
    market.set_bid("BTCUSDT", "81000")
    t = sim.close_position("BTCUSDT")
    assert t.exit_reason == fills.STRATEGY_EXIT and t.exit_price == Dec("81000") * Dec("0.995")
    assert not sim.positions and sim.close_position("BTCUSDT") is None
    assert sim.fills[-1].client_order_id.startswith("sbx0")


def test_close_position_lets_a_hit_stop_win():
    sim, market, _ = make()
    assert sim.submit(INTENT).filled
    market.set_bid("BTCUSDT", "77000")
    assert sim.close_position("BTCUSDT").exit_reason == fills.STOP_LOSS


# ---------------------------------------------------------------- engine: actions and edge mode
class ActsOnce:
    interval, name, symbols = "4h", "ACTS", ["BTCUSDT"]

    def __init__(self, enter_at, action):
        self.enter_at, self.action, self.done = enter_at, action, False

    def params(self):
        return {}

    def propose(self, data, now):
        if int(now * 1000) != self.enter_at:
            return []
        c = data.closed_candles("BTCUSDT", "4h", limit=1)[-1]
        e = entry_limit(c.close)
        return [make_intent("BTCUSDT", e, e * Dec("0.95"), "ACTS", 500, "test")]

    def manage(self, data, now, open_trades):
        if self.done or not open_trades:
            return []
        self.done = True
        return [self.action(open_trades[0])]


def test_exit_decided_at_a_close_fills_at_the_next_open():
    cs = {"BTCUSDT": walk(RESEARCH_START_MS - 100 * H4, 300, "7000", 3, vol=0.002)}
    k = 105
    s = ActsOnce(cs["BTCUSDT"][k].close_time + 1, lambda t: Exit(t.symbol, "test"))
    res = run(s, cs, rules(("BTCUSDT",)), AppConfig())
    t = res.trades[0]
    assert t.exit_reason == fills.STRATEGY_EXIT
    exit_open = cs["BTCUSDT"][k + 2]                                  # entry at k+1 open, decided at k+1 close
    assert t.closed_at == exit_open.open_time / 1000
    assert t.exit_price == exit_open.open * Dec("0.9999") * Dec("0.995")


def test_raise_stop_action_is_applied():
    cs = {"BTCUSDT": walk(RESEARCH_START_MS - 100 * H4, 300, "7000", 3, vol=0.002)}
    k = 105
    s = ActsOnce(cs["BTCUSDT"][k].close_time + 1, lambda t: RaiseStop(t.symbol, t.entry_price * Dec("0.99")))
    res = run(s, cs, rules(("BTCUSDT",)), AppConfig())
    assert res.trades and res.trades[0].exit_reason in (fills.STOP_LOSS, fills.TIME_STOP, fills.TAKE_PROFIT)
    assert res.trades[0].exit_price >= res.trades[0].entry_price * Dec("0.99") * Dec("0.99")


def crash():
    return {"BTCUSDT": walk(RESEARCH_START_MS - 200 * H4, 1200, "7000", 5, drift=-0.003, vol=0.02),
            "ETHUSDT": walk(RESEARCH_START_MS - 200 * H4, 1200, "130", 6, drift=-0.003, vol=0.02)}


def test_edge_mode_records_breaches_and_keeps_measuring():
    data = crash()
    strat = RandomEntry(["BTCUSDT", "ETHUSDT"], seed=0, chance_per_candle=0.5)
    dep = run(strat, data, rules(), AppConfig())
    edge = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=0, chance_per_candle=0.5), data, rules(), AppConfig(),
               edge=True)
    assert dep.drawdown_halt_ms is not None and not dep.drawdown_events
    assert len(edge.drawdown_events) >= 2 and edge.drawdown_halt_ms is None
    assert len(edge.trades) > len(dep.trades)
    assert edge.outcomes[RejectCode.MODE.value] < dep.outcomes[RejectCode.MODE.value]


# ---------------------------------------------------------------- walk-forward
def test_windows_cover_2022_to_the_holdout_and_never_touch_it():
    ws = windows()
    assert len(ws) == 8
    assert ws[0] == Window(ms(2020, 1), ms(2022, 1), ms(2022, 1), ms(2022, 7))
    assert ws[-1].test_start == ms(2025, 7) and ws[-1].test_end == HOLDOUT_START_MS
    assert all(w.train_end == w.test_start and w.train_start >= RESEARCH_START_MS for w in ws)
    assert all(a.test_end == b.test_start for a, b in zip(ws, ws[1:]))
    assert ws[0].label == "2022-01 to 2022-06" and ws[-1].label == "2025-07 to 2025-09"


def test_variants_and_sensitivity_sets():
    s = TrendBreakout(["BTCUSDT"])
    vs = variants(s)
    assert len(vs) == 27 and len({tuple(v.params().items()) for v in vs}) == 27
    assert {v.range_candles for v in vs} == {24, 30, 36}
    labels = [label for label, _ in one_at_a_time(s)]
    assert labels == ["range_candles x0.8", "range_candles x1.2", "atr_mult x0.8", "atr_mult x1.2",
                      "horizon_hours x0.8", "horizon_hours x1.2"]


def test_score_needs_enough_trades_and_rewards_return_per_drawdown():
    def m(trades, end, dd):
        from bot.backtest.metrics import Metrics
        return Metrics(Dec(10000), Dec(end), Dec(0), None, Dec(0), None, Dec(dd), Dec(0), Dec(1), Dec(0), trades=trades)
    assert score(m(19, 11000, "0.02")) is None
    assert score(m(20, 11000, "0.02")) == Dec(1000) / Dec("0.02")
    assert score(m(20, 11000, "0.001")) == Dec(1000) / Dec("0.01")        # drawdown floor 1%


# ---------------------------------------------------------------- selection rule, end to end on short windows
SHORT = [Window(ms(2020, 1), ms(2020, 4), ms(2020, 4), ms(2020, 5)),
         Window(ms(2020, 2), ms(2020, 5), ms(2020, 5), ms(2020, 6))]


def short_data():
    return {"BTCUSDT": walk(RESEARCH_START_MS - 300 * H4, 300 + 6 * 152, "7000", 21, vol=0.015),
            "ETHUSDT": walk(RESEARCH_START_MS - 300 * H4, 300 + 6 * 152, "130", 22, vol=0.015)}


@pytest.fixture(scope="module")
def evaluation():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(walkforward, "GRID", (Dec(1),))          # the 27-combination grid is tested above
        return selection.evaluate(TimeSeriesMomentum(["BTCUSDT", "ETHUSDT"]), short_data(), rules(), AppConfig(),
                                  random_runs=3, ws=SHORT)


def test_evaluation_covers_every_check(evaluation):
    names = [c.name for c in evaluation.checks]
    assert len(names) == 7 and any("doubled costs" in n for n in names) and any("6" in n for n in names)
    assert len(evaluation.windows) == 2 and len(evaluation.sensitivity) == 6
    assert set(evaluation.per_coin) == {"BTCUSDT", "ETHUSDT"} and len(evaluation.random_expectancies) <= 3
    assert evaluation.oos_trades == sum(len(r.edge.trades) for r in evaluation.windows)
    assert evaluation.passes_walk_forward == all(c.passed for c in evaluation.checks)


def test_fewer_than_100_trades_fails(evaluation):
    c = next(c for c in evaluation.checks if c.name.startswith("At least 100"))
    assert c.passed == (evaluation.oos_trades >= 100)


def test_report_renders_with_verdict_and_holdout_pending(evaluation):
    from bot.backtest import report
    meta = report.provenance(AppConfig(), short_data(), report.CostModel())
    text = selection.render(evaluation, meta, AppConfig())
    for must in ("Verdict:", "Selection rule", "Holdout (2025-10 to 2026-10) | pending", "Walk-forward test windows",
                 "Robustness", "Per coin", "By market regime", "Random entries with the same exits",
                 "Limits of simulation", "no such reset"):
        assert must in text, must
    assert selection.summary(evaluation)["passes_walk_forward"] == evaluation.passes_walk_forward


def test_concentration_checks():
    ev = selection.StrategyEvaluation(
        name="X", default_params={}, windows=[], oos_trades=150, oos_pnl=Dec(100), oos_expectancy=Dec(1),
        oos_doubled_trades=150, oos_doubled_expectancy=Dec("0.5"), full_edge=None, full_deployed=None,
        full_deployed_halt_ms=None, full_drawdown_events=0, sensitivity=[], per_coin={}, regimes={},
        random_expectancies=[], by_coin={"BTCUSDT": Dec(120), "ETHUSDT": Dec(-20)},
        by_month={"2023-01": Dec(150), "2023-02": Dec(-50)})
    ev.full_edge = compute([walk_point()], Dec(10000), 0)
    out = {c.name: c for c in selection.checks(ev, AppConfig())}
    assert not out["Not driven by one coin"].passed and "without BTCUSDT" in out["Not driven by one coin"].detail
    assert not out["Not driven by one month"].passed
    assert out["At least 100 out-of-sample trades"].passed


def walk_point():
    from bot.backtest.engine import EquityPoint
    return EquityPoint(86_400_000, Dec(10100), False)


# ---------------------------------------------------------------- CLI
def test_cli_run_writes_a_report(tmp_path, monkeypatch):
    from bot.backtest import history
    d = short_data()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for s, cs in d.items():
        history.write_csv(data_dir / f"{s}-4h.csv", cs)
        (data_dir / f"{s}-rules.json").write_text(__import__("json").dumps(history.rules_to_info(rules((s,))[s])))
    monkeypatch.setattr(walkforward, "windows", lambda: SHORT)
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))
    out = io.StringIO()
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(data_dir), interval="4h", strategy=["H2"],
                              random_runs=2, reports_dir=str(tmp_path / "rep"))
    assert cli._backtest_run(args, out=out) == 0
    text = out.getvalue()
    assert "H2: walk-forward window 2 of 2" in text and "Summary" in text
    assert list((tmp_path / "rep").glob("*-strategy-H2/report.md"))


def test_choose_picks_the_best_score_or_keeps_defaults(monkeypatch):
    from bot.backtest.metrics import Metrics

    s = TrendBreakout(["BTCUSDT"])
    w = SHORT[0]

    def fake_metrics(trades_for):
        def metrics_of(res):
            p = res
            n, profit = trades_for(p)
            return Metrics(Dec(10000), Dec(10000) + profit, Dec(0), None, Dec(0), None, Dec("0.02"), Dec(0), Dec(1),
                           Dec(0), trades=n)
        return metrics_of
    monkeypatch.setattr(walkforward, "run", lambda v, *a, **k: v)            # "result" = the variant itself
    monkeypatch.setattr(walkforward, "metrics_of", fake_metrics(lambda v: (50, Dec(v.range_candles * 10))))
    best, sc = walkforward.choose(s, {}, {}, AppConfig(), None, w)
    assert best.range_candles == 36 and sc == Dec(360) / Dec("0.02")
    monkeypatch.setattr(walkforward, "metrics_of", fake_metrics(lambda v: (5, Dec(1000))))   # too few trades
    best, sc = walkforward.choose(s, {}, {}, AppConfig(), None, w)
    assert best.params() == s.params() and sc is None
