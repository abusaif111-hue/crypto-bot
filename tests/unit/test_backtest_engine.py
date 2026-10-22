"""Module 4: the backtest engine replays history through the simulator and the risk engine."""
from dataclasses import replace
from decimal import Decimal as Dec

import pytest

import bot.paper.sim as sim_mod
from bot.backtest import benchmarks, report
from bot.backtest.engine import EquityPoint, _apply_resume_policy, run
from bot.backtest.metrics import compute, percentile
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS, HoldoutLocked
from bot.backtest.replay import CostModel
from bot.core.config import AppConfig
from bot.paper.sim import SimExchange
from bot.risk.models import Mode, RejectCode, TradeIntent
from bot.strategies.base import TAKE_PROFIT_R, AtrExit, entry_limit, make_intent
from bot.strategies.random_entry import RandomEntry
from tests.backtest_data import H4, rules, walk

START = RESEARCH_START_MS - 200 * H4


def two_coins(n=1500, drift=0.0):
    return {"BTCUSDT": walk(START, n, "7000", 1, drift=drift), "ETHUSDT": walk(START, n, "130", 2, drift=drift)}


class Scripted:
    """Proposes given intents at given close times, and records what it could see."""
    interval = "4h"
    name = "SCRIPTED"

    def __init__(self, plan=None):
        self.plan, self.seen = plan or {}, []

    def params(self):
        return {}

    def propose(self, data, now):
        latest = max(c.close_time for s in ("BTCUSDT", "ETHUSDT") for c in data.closed_candles(s, "4h", limit=5))
        self.seen.append((now, latest))
        return self.plan.get(int(now * 1000), [])

    def manage(self, data, now, open_trades):
        return []


def intent_at(c, stop_frac="0.97"):
    entry = entry_limit(c.close)
    return make_intent("BTCUSDT", entry, entry * Dec(stop_frac), "SCRIPTED", 96, "test")


def test_strategy_only_ever_sees_finished_candles():
    s = Scripted()
    run(s, two_coins(400), rules(), AppConfig())
    assert s.seen and all(latest < now * 1000 for now, latest in s.seen)


def test_idea_fills_at_the_next_open_with_spread_and_slippage(monkeypatch):
    cs = two_coins(400)
    k = next(i for i, c in enumerate(cs["BTCUSDT"]) if c.open_time >= RESEARCH_START_MS) + 5
    signal = cs["BTCUSDT"][k]
    calls = []
    real = sim_mod.evaluate
    monkeypatch.setattr(sim_mod, "evaluate", lambda *a, **kw: calls.append(a[0]) or real(*a, **kw))
    res = run(Scripted({signal.close_time + 1: [intent_at(signal)]}), cs, rules(), AppConfig())
    assert len(calls) == 1                                           # through the risk engine
    entry = res.fills[0]
    nxt = cs["BTCUSDT"][k + 1]
    assert entry.at == nxt.open_time / 1000
    assert entry.price == nxt.open * Dec("1.0001") * Dec("1.0005")   # ask (half-spread) + 0.05% slippage
    assert entry.fee == entry.qty * entry.price * Dec("0.001")


def test_gap_through_the_stop_fills_at_the_next_open():
    cs = two_coins(400)
    k = next(i for i, c in enumerate(cs["BTCUSDT"]) if c.open_time >= RESEARCH_START_MS) + 5
    btc = cs["BTCUSDT"]
    signal = btc[k]
    stop = entry_limit(signal.close) * Dec("0.97")
    # candle k+1 opens near the close (entry), candle k+2 opens 10% lower: a gap through the stop
    btc[k + 1] = replace(btc[k + 1], open=signal.close, high=signal.close * Dec("1.001"),
                         low=signal.close * Dec("0.999"), close=signal.close)
    gap_open = (signal.close * Dec("0.90")).quantize(Dec("0.01"))
    btc[k + 2] = replace(btc[k + 2], open=gap_open, high=gap_open, low=gap_open * Dec("0.99"), close=gap_open)
    res = run(Scripted({signal.close_time + 1: [intent_at(signal)]}), cs, rules(), AppConfig())
    t = res.trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.gapped
    assert t.exit_price == gap_open * Dec("0.9999") * Dec("0.995")  # bid at the open, 0.5% BTC stop slippage
    assert -t.pnl > t.planned_risk                                    # gaps can lose more than planned
    assert t.exit_price < stop


def test_runs_are_repeatable_and_seeds_differ():
    cs = two_coins(800)
    a = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=7), cs, rules(), AppConfig())
    b = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=7), cs, rules(), AppConfig())
    c = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=8), cs, rules(), AppConfig())
    def same(fills):                                         # order IDs are random by design; all else repeats
        return [replace(f, client_order_id="") for f in fills]
    assert same(a.fills) == same(b.fills) and a.equity == b.equity and a.fills
    assert same(a.fills) != same(c.fills)


def test_accounting_adds_up():
    res = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=3), two_coins(800), rules(), AppConfig())
    if not res.open_at_end:
        assert res.equity[-1].equity == Dec("10000") + sum((t.pnl for t in res.trades), Dec(0))
    assert all(f.fee == f.qty * f.price * Dec("0.001") for f in res.fills)
    assert all(f.slippage >= 0 for f in res.fills)


def test_holdout_candles_are_refused():
    cs = {"BTCUSDT": walk(HOLDOUT_START_MS - 10 * H4, 20, "60000")}
    with pytest.raises(HoldoutLocked):
        run(Scripted(), cs, rules(("BTCUSDT",)), AppConfig())


def test_doubled_costs_cost_more():
    cs = two_coins(800)
    a = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=1), cs, rules(), AppConfig())
    b = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=1), cs, rules(), AppConfig(), cost=CostModel().doubled())
    fa, fb = a.fills[0], b.fills[0]
    assert fb.fee / (fb.qty * fb.price) == Dec("0.002") and fb.slippage / fb.qty > fa.slippage / fa.qty


def test_drawdown_breaker_stops_the_run_and_is_reported():
    crash = {"BTCUSDT": walk(START, 900, "7000", 5, drift=-0.003, vol=0.02),
             "ETHUSDT": walk(START, 900, "130", 6, drift=-0.003, vol=0.02)}
    res = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=0, chance_per_candle=0.5), crash, rules(), AppConfig())
    assert res.drawdown_halt_ms is not None
    assert all(f.at * 1000 <= res.drawdown_halt_ms or f.side == "SELL" for f in res.fills)
    assert res.outcomes[RejectCode.MODE.value] > 0


def test_resume_policy_clears_daily_but_never_drawdown():
    from tests.fakes import Clock, FakeMarket

    clock = Clock()
    sim = SimExchange(AppConfig(), FakeMarket(), clock=clock)
    sim.resume("t", "start of the test run")
    sim.quote -= Dec("160")                                   # 1.6% lost today
    sim.account_state()
    assert sim.modes.mode is Mode.ENTRIES_BLOCKED
    _apply_resume_policy(sim, clock())
    assert sim.modes.mode is Mode.ENTRIES_BLOCKED            # same day: stays blocked
    clock.advance(86400)
    _apply_resume_policy(sim, clock())
    assert sim.modes.mode is Mode.RUNNING                    # next day: resumed
    sim.quote -= Dec("600")                                   # now 7.6% below the peak
    sim.account_state()
    for _ in range(10):
        clock.advance(86400 * 7)
        _apply_resume_policy(sim, clock())
    assert sim.modes.mode is Mode.ENTRIES_BLOCKED and RejectCode.DRAWDOWN.value in sim.modes.active_breaches


# ---------------------------------------------------------------- strategy helpers
def test_make_intent_sets_take_profit_at_three_times_the_stop_distance():
    i = make_intent("BTCUSDT", Dec("100"), Dec("95"), "X", 24, "x")
    assert i.take_profit_price == Dec("100") + TAKE_PROFIT_R * 5 == Dec("115")
    assert make_intent("BTCUSDT", Dec("100"), Dec("101"), "X", 24, "x") is None
    assert make_intent("BTCUSDT", Dec("100"), None, "X", 24, "x") is None
    assert entry_limit(Dec("1000")) == Dec("1001")
    assert AtrExit().stop_for(walk(START, 5), Dec("100")) is None


# ---------------------------------------------------------------- benchmarks and metrics
def test_benchmarks():
    cfg, cost = AppConfig(), CostModel()
    up = {"BTCUSDT": walk(RESEARCH_START_MS, 100, "100", 1, drift=0.01, vol=0.0001),
          "ETHUSDT": walk(RESEARCH_START_MS, 100, "100", 2, drift=0.01, vol=0.0001)}
    cash = benchmarks.cash(up, RESEARCH_START_MS, None, Dec("10000"))
    assert len(cash) == 100 and all(p.equity == 10000 for p in cash)
    hold = benchmarks.buy_and_hold(up, "BTCUSDT", cfg, cost, RESEARCH_START_MS, None, Dec("10000"))
    growth = up["BTCUSDT"][-1].close / up["BTCUSDT"][0].open
    assert hold[-1].equity < Dec("10000") * growth                   # costs paid
    assert hold[-1].equity > Dec("10000") * growth * Dec("0.995")
    ff = benchmarks.fifty_fifty_monthly(up, cfg, cost, RESEARCH_START_MS, None, Dec("10000"))
    assert ff[-1].equity > Dec("10000") * 2


def test_metrics_on_a_known_curve():
    day = 86_400_000
    pts = [EquityPoint(day * i, Dec(v), i % 2 == 0) for i, v in enumerate([10000, 11000, 9900, 12000], start=1)]
    m = compute(pts, Dec("10000"), 0)
    assert m.net_return == Dec("0.2") and m.max_drawdown == Dec("0.1") and m.max_drawdown_days == 2
    assert m.exposure == Dec("0.5") and m.days == 4
    never_back = compute(pts[:3], Dec("10000"), 0)
    assert never_back.max_drawdown_days == 1                     # peak on day 2, still below at the end (day 3)
    assert percentile([Dec(x) for x in range(11)], Dec("0.5")) == 5
    assert percentile([Dec(1), Dec(3)], Dec("0.25")) == Dec("1.5")


def test_metrics_with_trades():
    res = run(RandomEntry(["BTCUSDT", "ETHUSDT"], seed=2), two_coins(800), rules(), AppConfig())
    m = report.result_metrics(res)
    assert m.trades == len(res.trades) > 0
    assert m.fees == sum((f.fee for f in res.fills), Dec(0))
    assert sum(m.pnl_by_symbol.values(), Dec(0)) == sum((t.pnl for t in res.trades), Dec(0))
    assert m.gross_return >= m.net_return


# ---------------------------------------------------------------- report
def test_baselines_report(tmp_path):
    cs = two_coins(700)
    results = report.run_baselines(cs, rules(), AppConfig(), seeds=3)
    meta = report.provenance(AppConfig(), cs, CostModel())
    text = report.render(results, meta)
    for must in ("Buy and hold BTC", "50/50 BTC/ETH", "Random entries", "Limits of simulation", "Cost model",
                 "holdout locked", "Config fingerprint"):
        assert must in text, must
    folder = report.write(tmp_path, "baselines", results, meta)
    assert (folder / "report.md").exists() and (folder / "results.json").exists()
    assert len(meta["git_commit"]) >= 7


def test_irregular_candle_decisions_wait_for_its_real_close():
    cs = two_coins(400)
    k = next(i for i, c in enumerate(cs["BTCUSDT"]) if c.open_time >= RESEARCH_START_MS) + 5
    for s in cs:                                                    # a maintenance halt on both coins
        c = cs[s][k]
        cs[s][k] = replace(c, close_time=c.open_time + 2 * 3_600_000 - 1)
    s = Scripted()
    res = run(s, cs, rules(), AppConfig())
    short_close = cs["BTCUSDT"][k].close_time + 1
    assert any(p.at_ms == short_close for p in res.equity)          # marked at the real close
    assert all(latest < now * 1000 for now, latest in s.seen)
    long_cs = two_coins(400)
    for sym in long_cs:                                             # one coin's candle still open when the other's starts
        long_cs[sym] = [c for c in long_cs[sym]]
    long_cs["ETHUSDT"][k] = replace(long_cs["ETHUSDT"][k], open_time=long_cs["ETHUSDT"][k].open_time + 3_600_000)
    with pytest.raises(ValueError, match="overlap"):
        run(Scripted(), long_cs, rules(), AppConfig())
