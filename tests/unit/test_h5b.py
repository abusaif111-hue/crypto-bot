"""H5b (docs/h5b-plan.md, approved 9 Nov 2026): H5's daily breakout on more coins.

The candidates and coin rules are pinned here; the coin check is written once; H5's own code is unchanged;
the backtest adds check 8 (the new coins earn their place); nothing reads the spent holdout year.
Made-up data only."""
import argparse
import ast
import io
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot.backtest import h5b_coins, history, selection, universe, walkforward
from bot.backtest.engine import run
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS, WARMUP_START_MS
from bot.core.config import AppConfig
from bot.strategies.h5_breakout import DailyBreakout
from tests.backtest_data import H4, rules, walk
from tests.unit.test_universe_screen import BTC, BTC_RETURNS, DAY, N, daily_walk, rules_for
from tests.unit.test_walkforward_selection import SHORT

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"


def ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def test_the_candidates_and_rules_are_fixed():
    assert h5b_coins.CANDIDATES == ("SOLUSDT", "TRXUSDT", "ZECUSDT", "HYPEUSDT", "DOGEUSDT", "XMRUSDT")
    assert h5b_coins.BASE == ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT")
    assert h5b_coins.BASE_GROUPS == {"BTCUSDT": "majors", "ETHUSDT": "majors", "XRPUSDT": "XRPUSDT",
                                     "BNBUSDT": "BNBUSDT"}
    assert h5b_coins.HISTORY_BY_MS == ms(2021, 10, 1) and h5b_coins.MARGIN_MB == 30 and h5b_coins.INTERVAL == "4h"
    # the stability and liquidity rules are the screen's own, unchanged
    assert (universe.MAX_SHARE_ABOVE, universe.MAX_DRAWDOWN, universe.MIN_LIQUID_SHARE) == \
        (Dec("0.25"), Dec("0.70"), Dec("0.95"))
    assert universe.SCREEN_START_MS == ms(2022, 10, 1) and universe.SCREEN_END_MS == HOLDOUT_START_MS


def test_h5b_is_h5s_own_class_with_more_coins():
    s = cli._make_strategy("H5b", ["BTCUSDT", "SOLUSDT"])
    assert type(s) is DailyBreakout and s.name == "H5b" and s.symbols == ["BTCUSDT", "SOLUSDT"]
    assert s.params() == DailyBreakout(["BTCUSDT"]).params()
    assert DailyBreakout(["BTCUSDT"]).name == "H5"                           # H5 itself unchanged
    assert cli._make_strategy("H5", ["BTCUSDT"]).name == "H5"


# ---------------------------------------------------------------- the coin check
def wild(start, seed):
    return daily_walk(start, (HOLDOUT_START_MS - start) // DAY, "1", seed, vol=0.09)


def calm(start, seed, price="1", volume="50000000"):
    n = (HOLDOUT_START_MS - start) // DAY
    offset = (start - WARMUP_START_MS) // DAY
    return daily_walk(start, n, price, seed, vol=0.005, volume=volume, base=BTC_RETURNS[offset:offset + n])


class CoinVenue:
    """exchangeInfo for the listed pairs, daily klines, and the first 4h candle of each coin."""
    def __init__(self, daily, listed):
        self.daily, self.listed, self.calls = daily, listed, []

    def all_symbol_rules(self):
        return [rules_for(s) for s in self.listed] + [rules_for("XMRBTC")]

    def symbol_rules(self, symbols):
        return {s: rules_for(s) for s in symbols}

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.calls.append((symbol, interval, end_ms))
        cs = self.daily[symbol]
        if interval == "4h":                                   # only the first one is ever asked for
            c = cs[0]
            return [replace(c, close_time=c.open_time + H4 - 1)][:limit]
        sel = [c for c in cs if (start_ms is None or c.open_time >= start_ms) and (end_ms is None or c.open_time <= end_ms)]
        return sel[:limit]


def coin_world():
    daily = {"BTCUSDT": BTC,
             "SOLUSDT": calm(ms(2020, 8, 11), 31, volume="900000000"),           # passes, follows BTC: majors
             "TRXUSDT": calm(WARMUP_START_MS, 32, volume="5000000"),             # too little volume
             "ZECUSDT": wild(WARMUP_START_MS, 33),                               # too wild
             "HYPEUSDT": calm(ms(2024, 11, 29), 34),                             # listed too late
             "DOGEUSDT": daily_walk(WARMUP_START_MS, N, "0.01", 35, vol=0.005, volume="400000000")}  # passes, own group
    listed = ["BTCUSDT", "SOLUSDT", "TRXUSDT", "ZECUSDT", "HYPEUSDT", "DOGEUSDT"]   # no XMRUSDT
    return CoinVenue(daily, listed)


def with_universe(tmp_path, symbols=h5b_coins.BASE, groups=None):
    (tmp_path / "screen").mkdir(exist_ok=True)
    (tmp_path / "screen" / "universe.json").write_text(json.dumps(
        {"symbols": list(symbols), "groups": groups or dict(h5b_coins.BASE_GROUPS)}))


def test_the_coin_check_applies_every_rule_and_is_written_once(tmp_path):
    with_universe(tmp_path)
    venue = coin_world()
    rec = h5b_coins.decide(venue, tmp_path, AppConfig(), "Sultan", available=lambda: 400, now=lambda: 0)
    fails = {r["symbol"]: r["failures"] for r in rec["results"]}
    assert "rule 1" in fails["XMRUSDT"][0]
    assert "after 2021-10-01 (rule 2)" in fails["HYPEUSDT"][0]
    assert any("volume" in f for f in fails["TRXUSDT"])
    assert any("worst fall" in f or "volatility" in f for f in fails["ZECUSDT"])
    assert fails["SOLUSDT"] == [] and fails["DOGEUSDT"] == []                # SOL listed 2020: allowed
    assert rec["coins"] == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
    assert rec["new_coins"] == ["SOLUSDT", "DOGEUSDT"] and rec["left_out_for_memory"] == []
    assert rec["groups"]["SOLUSDT"] == "majors" and rec["groups"]["DOGEUSDT"] == "DOGEUSDT"
    assert rec["groups"]["XRPUSDT"] == "XRPUSDT" and rec["operator"] == "Sultan"
    assert h5b_coins.load(tmp_path) == rec
    with pytest.raises(h5b_coins.H5bCoinsError, match="never repeated"):
        h5b_coins.decide(venue, tmp_path, AppConfig(), "Sultan", available=lambda: 400)
    for f in (tmp_path / h5b_coins.FOLDER).glob("*-1d.csv"):                  # the holdout year never downloaded
        assert int(f.read_text().strip().splitlines()[-1].split(",")[6]) < HOLDOUT_START_MS
    assert all(end is None or end < HOLDOUT_START_MS for _, interval, end in venue.calls if interval == "1d")
    text = h5b_coins.render(rec)
    assert "SOLUSDT   PASS" in text and "HYPEUSDT  fail" in text and "H5b coins:" in text


def test_memory_leaves_out_the_least_traded_new_coins_first(tmp_path):
    with_universe(tmp_path)
    need5 = universe.memory_needed_mb(5) + h5b_coins.MARGIN_MB                  # room for one new coin only
    rec = h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "Sultan", available=lambda: need5)
    assert rec["new_coins"] == ["SOLUSDT"] and rec["left_out_for_memory"] == ["DOGEUSDT"]
    assert rec["passed"] == ["SOLUSDT", "DOGEUSDT"] and rec["coins"][:4] == list(h5b_coins.BASE)
    assert "passed, but left out: memory" in h5b_coins.render(rec)


def test_no_room_or_no_pass_means_no_new_coins(tmp_path):
    with_universe(tmp_path)
    rec = h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "Sultan", available=lambda: 10)
    assert rec["new_coins"] == [] and rec["coins"] == list(h5b_coins.BASE)
    assert "H5b is dropped" in h5b_coins.render(rec)


def test_the_check_refuses_a_different_base_or_no_memory_figure(tmp_path):
    with_universe(tmp_path, symbols=("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"))
    with pytest.raises(h5b_coins.H5bCoinsError, match="H5's coins"):
        h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "Sultan", available=lambda: 400)
    with_universe(tmp_path, groups={**h5b_coins.BASE_GROUPS, "XRPUSDT": "majors"})
    with pytest.raises(h5b_coins.H5bCoinsError, match="H5's coins"):
        h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "Sultan", available=lambda: 400)
    with_universe(tmp_path)
    with pytest.raises(h5b_coins.H5bCoinsError, match="available memory"):
        h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "Sultan", available=lambda: None)
    with pytest.raises(h5b_coins.H5bCoinsError, match="operator"):
        h5b_coins.decide(coin_world(), tmp_path, AppConfig(), "  ", available=lambda: 400)
    assert not (tmp_path / h5b_coins.DECISION_FILE).exists()


def test_a_damaged_or_missing_record_is_refused(tmp_path):
    with pytest.raises(h5b_coins.H5bCoinsError, match="not decided yet"):
        h5b_coins.load(tmp_path)
    (tmp_path / h5b_coins.DECISION_FILE).write_text(json.dumps(
        {"strategy": "H5b", "coins": list(h5b_coins.BASE) + ["PEPEUSDT"], "new_coins": ["PEPEUSDT"],
         "groups": {}}))
    with pytest.raises(h5b_coins.H5bCoinsError, match="damaged"):
        h5b_coins.load(tmp_path)


def test_the_listing_rule_is_a_parameter_and_the_screen_keeps_its_own():
    late = calm(ms(2020, 8, 11), 41)
    assert not universe.assess("SOLUSDT", late, BTC, AppConfig()).passed          # the H2 screen: 2019-10-01
    assert universe.assess("SOLUSDT", late, BTC, AppConfig(), listed_by=h5b_coins.HISTORY_BY_MS).passed


# ---------------------------------------------------------------- check 8
def evaluation(new_coins=(), dbl=None):
    from bot.backtest.metrics import compute
    from tests.unit.test_walkforward_selection import walk_point
    ev = selection.StrategyEvaluation(
        name="H5b", default_params={}, windows=[], oos_trades=150, oos_pnl=Dec(100), oos_expectancy=Dec(1),
        oos_doubled_trades=150, oos_doubled_expectancy=Dec("0.5"), full_edge=None, full_deployed=None,
        full_deployed_halt_ms=None, full_drawdown_events=0, sensitivity=[], per_coin={}, regimes={},
        random_expectancies=[], by_coin={"BTCUSDT": Dec(60), "SOLUSDT": Dec(40)}, by_month={"2023-01": Dec(100)},
        new_coins=tuple(new_coins), oos_doubled_by_coin=dbl or {})
    ev.full_edge = compute([walk_point()], Dec(10000), 0)
    return {c.name: c for c in selection.checks(ev, AppConfig())}


CHECK8 = "The new coins earn their place (out of sample, doubled costs)"


def test_check_8_needs_the_new_coins_to_make_money_together():
    ok = evaluation(["SOLUSDT", "DOGEUSDT"], {"BTCUSDT": Dec(-5), "SOLUSDT": Dec(30), "DOGEUSDT": Dec(-10)})
    assert ok[CHECK8].passed and "+20.00 USDT together" in ok[CHECK8].detail
    bad = evaluation(["SOLUSDT", "DOGEUSDT"], {"BTCUSDT": Dec(500), "SOLUSDT": Dec(10), "DOGEUSDT": Dec(-10)})
    assert not bad[CHECK8].passed                                  # zero is not "makes money"; BTC cannot carry it
    none = evaluation(["SOLUSDT"], {"BTCUSDT": Dec(500)})
    assert not none[CHECK8].passed and none[CHECK8].detail == "no trades on the new coins"


def test_check_8_only_for_a_strategy_that_adds_coins():
    plain = evaluation()
    assert CHECK8 not in plain and len(plain) == 7


# ---------------------------------------------------------------- the engine and a coin listed later
def test_a_coin_listed_later_simply_starts_later():
    start = RESEARCH_START_MS - 400 * H4
    early = walk(start, 400 + 6 * 200, "100", 51, drift=0.0015, vol=0.01)
    late_start = RESEARCH_START_MS + 60 * 6 * H4
    late = walk(late_start, 6 * 140, "5", 52, drift=0.0015, vol=0.01)
    cs = {"BTCUSDT": early, "SOLUSDT": late}
    res = run(DailyBreakout(["BTCUSDT", "SOLUSDT"], name="H5b"), cs, rules(("BTCUSDT", "SOLUSDT")), AppConfig(),
              start_ms=RESEARCH_START_MS, edge=True, corr_groups={"BTCUSDT": "majors", "SOLUSDT": "SOLUSDT"})
    assert all(f.at * 1000 >= late_start for f in res.fills if f.symbol == "SOLUSDT")
    alone = run(DailyBreakout(["BTCUSDT"]), {"BTCUSDT": early}, rules(("BTCUSDT",)), AppConfig(),
                start_ms=RESEARCH_START_MS, edge=True)
    first_sol = min([f.at for f in res.fills if f.symbol == "SOLUSDT"], default=None)
    def key(f):
        return f.symbol, f.side, f.kind, f.qty, f.price, f.at
    before = [key(f) for f in res.fills if f.symbol == "BTCUSDT" and (first_sol is None or f.at < first_sol)]
    assert before and before == [key(f) for f in alone.fills if first_sol is None or f.at < first_sol]


# ---------------------------------------------------------------- the commands
def h5b_world(tmp_path):
    """h5b-coins.json and 4h data: H5's four coins plus SOL (listed later)."""
    data = {"BTCUSDT": walk(RESEARCH_START_MS - 400 * H4, 400 + 6 * 152, "7000", 61, drift=0.001, vol=0.015),
            "ETHUSDT": walk(RESEARCH_START_MS - 400 * H4, 400 + 6 * 152, "130", 62, drift=0.001, vol=0.015),
            "XRPUSDT": walk(RESEARCH_START_MS - 400 * H4, 400 + 6 * 152, "2", 63, drift=0.001, vol=0.015),
            "BNBUSDT": walk(RESEARCH_START_MS - 400 * H4, 400 + 6 * 152, "15", 64, drift=0.001, vol=0.015),
            "SOLUSDT": walk(RESEARCH_START_MS - 300 * H4, 300 + 6 * 152, "10", 65, drift=0.001, vol=0.015)}
    for s, cs in data.items():
        history.write_csv(tmp_path / f"{s}-4h.csv", cs)
        (tmp_path / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(rules((s,))[s])))
    rec = {"strategy": "H5b", "coins": list(h5b_coins.BASE) + ["SOLUSDT"], "new_coins": ["SOLUSDT"],
           "groups": {**h5b_coins.BASE_GROUPS, "SOLUSDT": "SOLUSDT"}, "decided_at": "2026-11-09T00:00:00Z"}
    (tmp_path / h5b_coins.DECISION_FILE).write_text(json.dumps(rec))
    return data


def run_args(tmp_path, strategy=("H5b",)):
    return argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                              strategy=list(strategy), random_runs=1, reports_dir=str(tmp_path / "rep"),
                              screened=False)


def test_run_h5b_uses_the_recorded_coins_and_adds_check_8(tmp_path, monkeypatch):
    h5b_world(tmp_path)
    monkeypatch.setattr(walkforward, "windows", lambda: SHORT)
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)
    seen = {}
    real = selection.evaluate

    def spy(strategy, candles, *a, **k):
        seen.update(name=strategy.name, coins=sorted(candles), new=k.get("new_coins"), groups=k.get("corr_groups"))
        return real(strategy, candles, *a, **k)
    monkeypatch.setattr(selection, "evaluate", spy)
    out = io.StringIO()
    assert cli._backtest_run(run_args(tmp_path), out=out) == 0
    assert seen["name"] == "H5b" and seen["new"] == ["SOLUSDT"] and seen["groups"]["SOLUSDT"] == "SOLUSDT"
    assert seen["coins"] == sorted(list(h5b_coins.BASE) + ["SOLUSDT"])
    text = out.getvalue()
    assert CHECK8 in text and "# H5b backtest" in text and "new coins SOLUSDT" in text
    [summary] = (tmp_path / "rep").glob("*-strategy-H5b/results.json")
    saved = json.loads(summary.read_text())
    assert saved["new_coins"] == ["SOLUSDT"] and len(saved["checks"]) == 8
    # check 8 reads the doubled-cost trades: each coin's P&L there is below its normal-cost P&L
    single, doubled = saved["oos"]["by_coin"], saved["oos_doubled_by_coin"]
    assert single and set(doubled) == set(single)
    assert all(Dec(doubled[s]) < Dec(single[s]) for s in single)


def test_run_h5b_alone_and_only_with_new_coins(tmp_path, capsys):
    assert cli._backtest_run(run_args(tmp_path, ("H5", "H5b")), out=io.StringIO()) == 2
    assert "run H5b on its own" in capsys.readouterr().err
    assert cli._backtest_run(run_args(tmp_path), out=io.StringIO()) == 2
    assert "h5b-coins" in capsys.readouterr().err
    (tmp_path / h5b_coins.DECISION_FILE).write_text(json.dumps(
        {"strategy": "H5b", "coins": list(h5b_coins.BASE), "new_coins": [], "groups": dict(h5b_coins.BASE_GROUPS)}))
    assert cli._backtest_run(run_args(tmp_path), out=io.StringIO()) == 2
    assert "dropped without a backtest" in capsys.readouterr().err


def test_fetch_h5b_downloads_only_the_new_coins_and_stops_before_the_holdout(tmp_path):
    rec = {"strategy": "H5b", "coins": list(h5b_coins.BASE) + ["SOLUSDT"], "new_coins": ["SOLUSDT"],
           "groups": {**h5b_coins.BASE_GROUPS, "SOLUSDT": "SOLUSDT"}}
    (tmp_path / h5b_coins.DECISION_FILE).write_text(json.dumps(rec))
    sol = walk(ms(2020, 8, 11), (HOLDOUT_START_MS + 60 * DAY - ms(2020, 8, 11)) // H4, "3", 71)

    class Venue:
        calls = []

        def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
            self.calls.append((symbol, interval, end_ms))
            return [c for c in sol if c.open_time >= start_ms and (end_ms is None or c.open_time <= end_ms)][:limit]

        def symbol_rules(self, symbols):
            return {s: rules_for(s) for s in symbols}
    v = Venue()
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h", symbols=None,
                              screened=False, h5b=True)
    assert cli._backtest_fetch(args, make_exchange=lambda **k: v) == 0
    assert {s for s, _, _ in v.calls} == {"SOLUSDT"}
    assert all(end is not None and end < HOLDOUT_START_MS for _, _, end in v.calls)
    saved = history.read_csv(tmp_path / "SOLUSDT-4h.csv")
    assert saved and saved[-1].close_time < HOLDOUT_START_MS
    assert cli._backtest_fetch(replace_args(args, interval="1h"), make_exchange=lambda **k: v) == 2


def replace_args(args, **k):
    return argparse.Namespace(**{**vars(args), **k})


def test_the_coin_check_command(tmp_path, monkeypatch):
    with_universe(tmp_path)
    monkeypatch.setattr(h5b_coins, "available_memory_mb", lambda: 400)
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), operator="Sultan")
    out = io.StringIO()
    assert cli._backtest_h5b_coins(args, make_exchange=lambda **k: coin_world(), out=out) == 0
    assert "Next: bot backtest fetch --h5b" in out.getvalue()
    assert cli._backtest_h5b_coins(args, make_exchange=lambda **k: coin_world(), out=io.StringIO()) == 2


def test_h5b_code_never_opens_the_holdout():
    tree = ast.parse((SRC / "backtest" / "h5b_coins.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    for bad in ("load_through_holdout", "load_with_forward", "HoldoutTicket", "open_holdout", "HOLDOUT_END_MS"):
        assert bad not in names, bad
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith(("bot.execution", "bot.exchange.trading")) for m in mods)


def test_the_command_reads_the_memory_figure_when_it_runs(tmp_path, monkeypatch):
    """The coin check must use the memory available at the time it runs (and a test's stand-in for it)."""
    with_universe(tmp_path)
    monkeypatch.setattr(h5b_coins, "available_memory_mb", lambda: 10)
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), operator="Sultan")
    assert cli._backtest_h5b_coins(args, make_exchange=lambda **k: coin_world(), out=io.StringIO()) == 0
    assert h5b_coins.load(tmp_path)["left_out_for_memory"] == ["DOGEUSDT", "SOLUSDT"]   # least traded first
