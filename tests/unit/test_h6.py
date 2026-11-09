"""H6 (`bot.strategies.h6_rotation`, docs/h6-plan.md, approved 9 Nov 2026): weekly rotation into the strongest
coins. The fixed values are pinned here; changing one is a new strategy. Also: the one-time coin check (screen
waived by Sultan's decision 1), the extra coins' 2.0% stop slippage, and the stricter checks 7 (99th
percentile), 8 and 9. Made-up candles only."""
import argparse
import ast
import io
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as Dec
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import cli
from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.backtest import h6_coins, history, selection, universe, walkforward
from bot.backtest.engine import run
from bot.backtest.metrics import compute
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS, WARMUP_START_MS
from bot.core.config import AppConfig
from bot.exchange.interface import Candle
from bot.strategies import h6_rotation as h6
from bot.strategies.base import MAX_TUNABLE, Exit, OpenTrade
from tests.backtest_data import H4, rules, walk
from tests.unit.test_h5b import CoinVenue, calm, ms, wild, with_universe
from tests.unit.test_universe_screen import BTC, DAY, N, daily_walk
from tests.unit.test_walkforward_selection import SHORT, walk_point

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
MONDAY = ms(2021, 3, 1)                 # a Monday 00:00 UTC


def test_the_fixed_values_and_three_tunable_settings():
    s = h6.WeeklyRotation(["BTCUSDT"])
    assert s.params() == {"lookback_days": 28, "trend_days": 100, "stop_atr": Dec("2.5")}
    assert len(s.params()) <= MAX_TUNABLE and s.name == "H6" and s.interval == "4h"
    assert (h6.TOP_N, h6.ATR_DAYS, h6.HORIZON_DAYS) == (2, 20, 35)
    assert h6_coins.EXTRA == ("SOLUSDT", "DOGEUSDT", "TRXUSDT", "ZECUSDT")
    assert h6_coins.EXTRA_STOP_SLIPPAGE_PCT == Dec("2.0") and selection.H6_RANDOM_PERCENTILE == Dec("0.99")
    assert cli._make_strategy("H6", ["BTCUSDT"]) == h6.WeeklyRotation(["BTCUSDT"])


def test_the_ranking_happens_once_a_week_at_monday_midnight():
    assert datetime.fromtimestamp(MONDAY / 1000, timezone.utc).weekday() == 0
    t = MONDAY / 1000
    assert h6.WeeklyRotation.new_week(t)                                  # the close at Monday 00:00
    for later in (4 * 3600, DAY // 1000, 3 * DAY // 1000, 6 * DAY // 1000 + 20 * 3600):
        assert not h6.WeeklyRotation.new_week(t + later)
    assert h6.WeeklyRotation.new_week(t + 7 * DAY // 1000)


# ---------------------------------------------------------------- scores and leaders
def days_from(closes, start=MONDAY - 200 * DAY_MS):
    out = []
    for i, c in enumerate(closes):
        c = Dec(str(c))
        t = start + i * DAY_MS
        out.append(Candle(t, c, c * Dec("1.01"), c * Dec("0.99"), c, Dec(1), t + DAY_MS - 1, Dec("5e7"), True))
    return out


def four_hours(closes, start=MONDAY - 200 * DAY_MS):
    """Each daily close as six 4h candles, ending at MONDAY."""
    out, prev = [], Dec(str(closes[0]))
    for i, c in enumerate(closes):
        c = Dec(str(c))
        for k in range(6):
            t = start + i * DAY_MS + k * H4
            p = prev + (c - prev) * (k + 1) / 6
            o = out[-1].close if out else p
            out.append(Candle(t, o, max(o, p) * Dec("1.005"), min(o, p) * Dec("0.995"), p, Dec(1), t + H4 - 1,
                              Dec("5e7"), True))
        prev = c
    return out


def test_score_is_the_return_only_in_an_uptrend():
    s = h6.WeeklyRotation(["X"])
    up = days_from([100 + i for i in range(150)])
    assert s.score(up) == Dec(249) / Dec(221) - 1                         # 28-day return
    down = days_from([300 - i for i in range(150)])
    assert s.score(down) is None                                          # below its 100-day average
    assert s.score(up[:99]) is None                                       # not enough days
    flat = days_from([100] * 150)
    assert s.score(flat) is None                                          # at the average is not above it


class Source:
    def __init__(self, by):
        self.by = by

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        return self.by[symbol][-limit:]


def world():
    n = 200
    return {"BTCUSDT": four_hours([100 * (1.001 ** i) for i in range(n)]),           # slow rise
            "SOLUSDT": four_hours([10 * (1.004 ** i) for i in range(n)]),            # fastest rise
            "DOGEUSDT": four_hours([1 * (1.002 ** i) for i in range(n)]),            # middle
            "ZECUSDT": four_hours([50 * (0.997 ** i) for i in range(n)])}            # falling: never held


def test_the_top_two_in_an_uptrend_lead():
    s = h6.WeeklyRotation(["BTCUSDT", "SOLUSDT", "DOGEUSDT", "ZECUSDT"])
    assert s.leaders(Source(world())) == ["SOLUSDT", "DOGEUSDT"]
    only_falling = h6.WeeklyRotation(["ZECUSDT"])
    assert only_falling.leaders(Source(world())) == []                    # nothing in an uptrend: cash
    same = {"AUSDT": world()["SOLUSDT"], "BUSDT": world()["SOLUSDT"], "CUSDT": world()["SOLUSDT"]}
    assert h6.WeeklyRotation(["AUSDT", "BUSDT", "CUSDT"]).leaders(Source(same)) == ["AUSDT", "BUSDT"]   # ties


def test_propose_buys_the_leaders_with_atr_stop_3r_target_and_35_day_stop():
    s = h6.WeeklyRotation(["BTCUSDT", "SOLUSDT", "DOGEUSDT", "ZECUSDT"])
    src = Source(world())
    out = s.propose(src, MONDAY / 1000)
    assert [i.symbol for i in out] == ["SOLUSDT", "DOGEUSDT"]
    i = out[0]
    cs = world()["SOLUSDT"][-s.history_needed():]
    a = atr(daily(cs)[-60:], 20)[-1]
    assert i.entry_price == cs[-1].close * Dec("1.001") and i.stop_price == i.entry_price - Dec("2.5") * a
    assert i.take_profit_price == i.entry_price + 3 * (i.entry_price - i.stop_price)
    assert i.horizon_hours == 35 * 24 and i.trigger_price is None and i.strategy == "H6"
    assert s.propose(src, MONDAY / 1000 + 4 * 3600) == []                 # not again that week


def test_manage_sells_what_left_the_top_two_at_the_weekly_ranking():
    s = h6.WeeklyRotation(["BTCUSDT", "SOLUSDT", "DOGEUSDT", "ZECUSDT"])
    held = [OpenTrade(c, Dec(1), Dec("0.5"), 0) for c in ("BTCUSDT", "SOLUSDT", "ZECUSDT")]
    out = s.manage(Source(world()), MONDAY / 1000, held)
    assert {a.symbol for a in out} == {"BTCUSDT", "ZECUSDT"} and all(isinstance(a, Exit) for a in out)
    assert s.manage(Source(world()), MONDAY / 1000 + DAY // 1000, held) == []     # only at the ranking


def test_h6_is_pure_and_cannot_reach_orders_or_other_strategies():
    tree = ast.parse((SRC / "strategies" / "h6_rotation.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.strategies.h2b_blend",
                "bot.strategies.h5_breakout", "bot.forward", "bot.paper", "bot.backtest"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad


def test_a_backtest_holds_the_strongest_coins_and_rotates_weekly():
    start = RESEARCH_START_MS - 130 * 6 * H4
    n = 130 * 6 + 6 * 140
    cs = {"BTCUSDT": walk(start, n, "100", 81, drift=-0.0004, vol=0.004),               # falling: never held
          "SOLUSDT": walk(start, n, "10", 82, drift=0.0012, vol=0.004),
          "DOGEUSDT": walk(start, n, "1", 83, drift=0.0008, vol=0.004)}
    groups = {"BTCUSDT": "majors", "SOLUSDT": "SOLUSDT", "DOGEUSDT": "DOGEUSDT"}
    res = run(h6.WeeklyRotation(list(cs)), cs, rules(tuple(cs)), AppConfig(universe=list(cs)), start_ms=RESEARCH_START_MS,
              edge=True, corr_groups=groups)
    entries = [f for f in res.fills if f.kind == "ENTRY"]
    assert entries and {f.symbol for f in entries} <= {"SOLUSDT", "DOGEUSDT"}                # the two strongest
    for f in entries:                                       # bought at the open after a Monday 00:00 ranking
        assert (int(f.at * 1000) - h6.MONDAY_MS) % h6.WEEK_MS == 0


# ---------------------------------------------------------------- the coin check
def coin_world():
    daily_data = {"BTCUSDT": BTC,
                  "SOLUSDT": calm(ms(2020, 8, 11), 31, volume="900000000"),
                  "DOGEUSDT": wild(WARMUP_START_MS, 35),                                   # too jumpy: waived
                  "TRXUSDT": daily_walk(WARMUP_START_MS, N, "0.05", 32, vol=0.01, volume="5000000"),  # thin: waived
                  "ZECUSDT": calm(ms(2022, 3, 1), 33)}                                     # rule 2 still applies
    return CoinVenue(daily_data, list(daily_data))


def test_the_coin_check_waives_the_screen_but_keeps_rules_1_and_2(tmp_path):
    with_universe(tmp_path)
    venue = coin_world()
    rec = h6_coins.decide(venue, tmp_path, AppConfig(), "Sultan", available=lambda: 400, now=lambda: 0)
    by = {r["symbol"]: r for r in rec["results"]}
    assert rec["extra_coins"] == ["SOLUSDT", "DOGEUSDT", "TRXUSDT"]
    assert "rule 2" in by["ZECUSDT"]["failures"][0]
    assert by["DOGEUSDT"]["passed"] and any("volatility" in w or "worst fall" in w for w in by["DOGEUSDT"]["waived"])
    assert by["TRXUSDT"]["passed"] and any("volume" in w for w in by["TRXUSDT"]["waived"])
    assert rec["stop_slippage_pct"] == {"SOLUSDT": "2.0", "DOGEUSDT": "2.0", "TRXUSDT": "2.0"}
    assert rec["groups"]["SOLUSDT"] == "majors" and rec["groups"]["TRXUSDT"] == "TRXUSDT"
    assert rec["coins"][:4] == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT"]
    assert h6_coins.load(tmp_path) == rec
    with pytest.raises(h6_coins.H6CoinsError, match="never repeated"):
        h6_coins.decide(venue, tmp_path, AppConfig(), "Sultan", available=lambda: 400)
    for f in (tmp_path / h6_coins.FOLDER).glob("*-1d.csv"):
        assert int(f.read_text().strip().splitlines()[-1].split(",")[6]) < HOLDOUT_START_MS
    text = h6_coins.render(rec)
    assert "waived" in text and "ZECUSDT   out" in text and "H6 coins:" in text


def test_the_coin_check_refuses_an_untradeable_coin_and_obeys_memory(tmp_path):
    with_universe(tmp_path)
    v = coin_world()
    v.listed = [s for s in v.listed if s != "SOLUSDT"]                    # SOL not trading
    need5 = universe.memory_needed_mb(5) + h6_coins.MARGIN_MB             # room for one extra coin
    rec = h6_coins.decide(v, tmp_path, AppConfig(), "Sultan", available=lambda: need5)
    by = {r["symbol"]: r for r in rec["results"]}
    assert "rule 1" in by["SOLUSDT"]["failures"][0]
    assert rec["extra_coins"] == ["DOGEUSDT"] and rec["left_out_for_memory"] == ["TRXUSDT"]   # least traded first


def test_the_command_reads_memory_when_it_runs_and_refuses_a_wrong_base(tmp_path, monkeypatch):
    with_universe(tmp_path, symbols=("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"))
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), operator="Sultan")
    assert cli._backtest_h6_coins(args, make_exchange=lambda **k: coin_world(), out=io.StringIO()) == 2
    with_universe(tmp_path)
    monkeypatch.setattr(h6_coins, "available_memory_mb", lambda: 10)
    out = io.StringIO()
    assert cli._backtest_h6_coins(args, make_exchange=lambda **k: coin_world(), out=out) == 0
    assert h6_coins.load(tmp_path)["extra_coins"] == [] and "Next: bot backtest fetch --h6" in out.getvalue()


def test_a_damaged_record_is_refused(tmp_path):
    with pytest.raises(h6_coins.H6CoinsError, match="not decided yet"):
        h6_coins.load(tmp_path)
    bad = {"strategy": "H6", "coins": list(h6_coins.BASE) + ["SOLUSDT"], "extra_coins": ["SOLUSDT"],
           "groups": {**h6_coins.BASE_GROUPS, "SOLUSDT": "SOLUSDT"}, "stop_slippage_pct": {"SOLUSDT": "1.0"}}
    (tmp_path / h6_coins.DECISION_FILE).write_text(json.dumps(bad))
    with pytest.raises(h6_coins.H6CoinsError, match="damaged"):            # the 2.0% allowance cannot be lowered
        h6_coins.load(tmp_path)


def test_the_backtest_config_only_tightens_the_stop_allowance():
    rec = {"coins": list(h6_coins.BASE) + ["SOLUSDT"], "stop_slippage_pct": {"SOLUSDT": "2.0"}}
    base = AppConfig()
    cfg = h6_coins.backtest_config(base, rec)
    assert cfg.universe == rec["coins"] and cfg.risk.stop_slippage_for("SOLUSDT") == Dec("2.0")
    assert cfg.risk.stop_slippage_for("BTCUSDT") == base.risk.stop_slippage_for("BTCUSDT")
    assert cfg.risk.risk_per_trade_pct == base.risk.risk_per_trade_pct
    big = AppConfig.model_validate({**base.model_dump(), "risk": {**base.risk.model_dump(),
                                                                  "stop_slippage_pct": {"SOLUSDT": "3.0"}}})
    assert h6_coins.backtest_config(big, rec).risk.stop_slippage_for("SOLUSDT") == Dec("3.0")   # never lowered


# ---------------------------------------------------------------- checks 7 (99th), 8 and 9
def evaluation(**kw):
    ev = selection.StrategyEvaluation(
        name="H6", default_params={}, windows=[], oos_trades=150, oos_pnl=Dec(100), oos_expectancy=Dec(1),
        oos_doubled_trades=150, oos_doubled_expectancy=Dec("0.5"), full_edge=None, full_deployed=None,
        full_deployed_halt_ms=None, full_drawdown_events=0, sensitivity=[], per_coin={}, regimes={},
        random_expectancies=[Dec(i) for i in range(100)], by_coin={"BTCUSDT": Dec(60), "SOLUSDT": Dec(40)},
        by_month={"2023-01": Dec(100)}, **kw)
    ev.full_edge = replace(compute([walk_point()], Dec(10000), 0), expectancy=Dec("96.5"))   # 95th < it < 99th
    return {c.name: c for c in selection.checks(ev, AppConfig())}


def test_check_7_at_the_99th_percentile_is_stricter():
    assert evaluation()["Beats random entries with the same exits (95th percentile)"].passed
    strict = evaluation(random_percentile=Dec("0.99"))
    assert not strict["Beats random entries with the same exits (99th percentile)"].passed
    assert "Beats random entries with the same exits (95th percentile)" not in strict


CHECK9 = "Better than holding the same coins (return and drawdown, out of sample)"


def test_check_9_needs_both_a_better_return_and_a_smaller_drawdown():
    def hold(sr, hr, sd, hd):
        return evaluation(hold={"strategy_return": Dec(sr), "hold_return": Dec(hr),
                                "strategy_drawdown": Dec(sd), "hold_drawdown": Dec(hd)})[CHECK9]
    assert hold("0.30", "0.20", "0.05", "0.40").passed
    assert not hold("0.10", "0.20", "0.05", "0.40").passed               # less return
    assert not hold("0.30", "0.20", "0.40", "0.40").passed               # no smaller drawdown
    assert CHECK9 not in evaluation()                                    # only for H6


def test_hold_comparison_compounds_windows_against_equal_weight_holding():
    w1 = walkforward.Window(0, 0, RESEARCH_START_MS, RESEARCH_START_MS + 30 * DAY_MS)
    w2 = walkforward.Window(0, 0, w1.test_end, w1.test_end + 30 * DAY_MS)
    n = (w2.test_end - RESEARCH_START_MS) // H4
    up = walk(RESEARCH_START_MS, n, "100", 91, drift=0.002, vol=0.0)       # rises smoothly
    flat = walk(RESEARCH_START_MS, n, "100", 92, drift=0.0, vol=0.0)
    m = SimpleNamespace(start_equity=Dec(10000), end_equity=Dec(11000), max_drawdown=Dec("0.02"))
    wf = [SimpleNamespace(window=w, metrics={"edge": m}) for w in (w1, w2)]
    h = selection.hold_comparison(wf, {"AUSDT": up, "BUSDT": flat}, AppConfig(), selection.CostModel())
    assert h["strategy_return"] == Dec("1.21") - 1 and h["strategy_drawdown"] == Dec("0.02")
    half_up = (Dec("1.002") ** (30 * 6) - 1) / 2                             # half the money in the rising coin
    assert abs(h["hold_return"] - ((1 + half_up) ** 2 - 1)) < Dec("0.03")   # less the costs of buying twice
    assert h["hold_drawdown"] < Dec("0.01")


# ---------------------------------------------------------------- the commands
def h6_world(tmp_path):
    data = {s: walk(RESEARCH_START_MS - 700 * H4, 700 + 6 * 152, p, seed, drift=d, vol=0.01)
            for s, p, seed, d in (("BTCUSDT", "7000", 101, 0.0005), ("ETHUSDT", "130", 102, 0.0004),
                                  ("XRPUSDT", "2", 103, 0.0002), ("BNBUSDT", "15", 104, 0.0003),
                                  ("SOLUSDT", "10", 105, 0.0009))}
    for s, cs in data.items():
        history.write_csv(tmp_path / f"{s}-4h.csv", cs)
        (tmp_path / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(rules((s,))[s])))
    rec = {"strategy": "H6", "coins": list(h6_coins.BASE) + ["SOLUSDT"], "extra_coins": ["SOLUSDT"],
           "groups": {**h6_coins.BASE_GROUPS, "SOLUSDT": "SOLUSDT"}, "stop_slippage_pct": {"SOLUSDT": "2.0"},
           "decided_at": "2026-11-09T00:00:00Z"}
    (tmp_path / h6_coins.DECISION_FILE).write_text(json.dumps(rec))


def run_args(tmp_path, strategy=("H6",)):
    return argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                              strategy=list(strategy), random_runs=2, reports_dir=str(tmp_path / "rep"),
                              screened=False)


def test_run_h6_uses_the_record_the_stricter_checks_and_the_stop_allowance(tmp_path, monkeypatch):
    h6_world(tmp_path)
    monkeypatch.setattr(walkforward, "windows", lambda: SHORT)
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)
    seen = {}
    real = selection.evaluate

    def spy(strategy, candles, rules_, cfg, *a, **k):
        seen.update(name=strategy.name, coins=sorted(candles), kw=k, slip=cfg.risk.stop_slippage_for("SOLUSDT"))
        return real(strategy, candles, rules_, cfg, *a, **k)
    monkeypatch.setattr(selection, "evaluate", spy)
    out = io.StringIO()
    assert cli._backtest_run(run_args(tmp_path), out=out) == 0
    assert seen["name"] == "H6" and seen["coins"] == sorted(list(h6_coins.BASE) + ["SOLUSDT"])
    assert seen["kw"]["random_percentile"] == Dec("0.99") and seen["kw"]["hold_benchmark"] is True
    assert seen["kw"]["new_coins"] == ["SOLUSDT"] and seen["slip"] == Dec("2.0")
    text = out.getvalue()
    assert "(99th percentile)" in text and CHECK9 in text and "earn their place" in text and "# H6 backtest" in text
    [saved] = (tmp_path / "rep").glob("*-strategy-H6/results.json")
    data = json.loads(saved.read_text())
    assert len(data["checks"]) == 9 and data["random_percentile"] == "0.99" and data["hold"] is not None


def test_run_h6_alone_and_only_with_its_record(tmp_path, capsys):
    assert cli._backtest_run(run_args(tmp_path, ("H5", "H6")), out=io.StringIO()) == 2
    assert "run H6 on its own" in capsys.readouterr().err
    assert cli._backtest_run(run_args(tmp_path), out=io.StringIO()) == 2
    assert "h6-coins" in capsys.readouterr().err


def test_fetch_h6_downloads_only_the_extra_coins_and_stops_before_the_holdout(tmp_path):
    rec = {"strategy": "H6", "coins": list(h6_coins.BASE) + ["DOGEUSDT"], "extra_coins": ["DOGEUSDT"],
           "groups": {**h6_coins.BASE_GROUPS, "DOGEUSDT": "DOGEUSDT"}, "stop_slippage_pct": {"DOGEUSDT": "2.0"}}
    (tmp_path / h6_coins.DECISION_FILE).write_text(json.dumps(rec))
    doge = walk(ms(2021, 6, 1), (HOLDOUT_START_MS + 60 * DAY - ms(2021, 6, 1)) // H4, "30", 111)

    class Venue:
        calls = []

        def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
            self.calls.append((symbol, end_ms))
            return [c for c in doge if c.open_time >= start_ms and (end_ms is None or c.open_time <= end_ms)][:limit]

        def symbol_rules(self, symbols):
            from tests.unit.test_universe_screen import rules_for
            return {s: rules_for(s) for s in symbols}
    v = Venue()
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h", symbols=None,
                              screened=False, h5b=False, h6=True)
    assert cli._backtest_fetch(args, make_exchange=lambda **k: v) == 0
    assert {s for s, _ in v.calls} == {"DOGEUSDT"} and all(e is not None and e < HOLDOUT_START_MS for _, e in v.calls)
    assert history.read_csv(tmp_path / "DOGEUSDT-4h.csv")[-1].close_time < HOLDOUT_START_MS


def test_h6_coin_check_never_opens_the_holdout():
    tree = ast.parse((SRC / "backtest" / "h6_coins.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    for bad in ("load_through_holdout", "load_with_forward", "HoldoutTicket", "open_holdout", "HOLDOUT_END_MS"):
        assert bad not in names, bad
