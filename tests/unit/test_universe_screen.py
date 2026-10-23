"""Coin screen for the wider H2 re-test, correlation groups (R-08), and the --screened commands."""
import argparse
import io
import json
import random
from dataclasses import replace
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.backtest import universe, walkforward
from bot.backtest.periods import HOLDOUT_START_MS, RESEARCH_START_MS, WARMUP_START_MS
from bot.backtest.universe import (
    CoinResult, LISTED_BY_MS, SCREEN_START_MS, assess, correlation, groups, is_candidate, max_drawdown, pick,
)
from bot.core.config import AppConfig
from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Candle
from bot.paper.sim import SimExchange
from bot.risk.models import RejectCode, TradeIntent
from tests.conftest import exchange_info_symbol
from tests.fakes import Clock, FakeMarket

DAY = 86_400_000


def rules_for(symbol, status="TRADING"):
    info = exchange_info_symbol(symbol, status=status)
    info["baseAsset"] = symbol[:-4]
    return parse_symbol(info)


def daily_walk(start_ms, n, price, seed, vol=0.03, volume="50000000", base=None):
    """Daily candles; with `base` (a list of daily returns) the coin follows it plus noise."""
    rng = random.Random(seed)
    p, out = Dec(price), []
    for i in range(n):
        r = (base[i] if base else 0.0) + rng.gauss(0, vol)
        c = max((p * Dec(str(1 + r))).quantize(Dec("0.00000001")), Dec("0.00000001"))
        hi, lo = max(p, c) * Dec("1.01"), min(p, c) * Dec("0.99")
        t = start_ms + i * DAY
        out.append(Candle(t, p, hi, lo, c, Dec(1), t + DAY - 1, Dec(volume), True))
        p = c
    return out


N = (HOLDOUT_START_MS - WARMUP_START_MS) // DAY
_rng = random.Random(99)
BTC_RETURNS = [_rng.gauss(0.0005, 0.02) for _ in range(N)]
BTC = daily_walk(WARMUP_START_MS, N, "4000", 1, vol=0.0, base=BTC_RETURNS)


# ---------------------------------------------------------------- candidate rules
@pytest.mark.parametrize("symbol, ok", [
    ("XRPUSDT", True), ("BTCUSDT", False), ("ETHUSDT", False), ("USDCUSDT", False), ("FDUSDUSDT", False),
    ("EURUSDT", False), ("PAXGUSDT", False), ("WBTCUSDT", False), ("BTCUPUSDT", False), ("ETHDOWNUSDT", False),
    ("JUPUSDT", True), ("XRPBTC", False),
])
def test_candidates(symbol, ok):
    r = rules_for(symbol) if symbol.endswith("USDT") else replace(rules_for("XRPUSDT"), symbol="XRPBTC",
                                                                    quote_asset="BTC")
    assert is_candidate(r) is ok


def test_halted_pairs_are_not_candidates():
    assert not is_candidate(rules_for("XRPUSDT", status="BREAK"))


# ---------------------------------------------------------------- the screen
def test_a_calm_liquid_coin_passes_and_is_grouped_by_correlation():
    follower = daily_walk(WARMUP_START_MS, N, "0.5", 2, vol=0.005, base=BTC_RETURNS)       # moves with BTC
    loner = daily_walk(WARMUP_START_MS, N, "0.5", 3, vol=0.03)                             # its own path
    cfg = AppConfig()
    f, lo = assess("FOLUSDT", follower, BTC, cfg), assess("LONUSDT", loner, BTC, cfg)
    assert f.passed and lo.passed, (f.failures, lo.failures)
    assert f.corr_with_btc > Dec("0.9") and abs(lo.corr_with_btc) < Dec("0.3")
    g = groups([f, lo], cfg)
    assert g == {"BTCUSDT": "majors", "ETHUSDT": "majors", "FOLUSDT": "majors", "LONUSDT": "LONUSDT"}


def test_volatile_coins_fail_the_2x_btc_rule():
    wild = daily_walk(WARMUP_START_MS, N, "1", 4, vol=0.09)
    r = assess("WILDUSDT", wild, BTC, AppConfig())
    assert not r.passed and "2x BTC" in r.failures[0]


def test_a_70_percent_fall_fails():
    crash = daily_walk(WARMUP_START_MS, N, "100", 5, vol=0.0, base=[0.0] * N)
    k = next(i for i, c in enumerate(crash) if c.open_time >= SCREEN_START_MS) + 100
    crash = crash[:k] + [replace(c, open=Dec("20"), high=Dec("21"), low=Dec("19"), close=Dec("20")) for c in crash[k:]]
    r = assess("CRASHUSDT", crash, BTC, AppConfig())
    assert r.max_drawdown == Dec("0.8") and any("worst fall" in x for x in r.failures)


def test_thin_trading_fails_the_liquidity_rule():
    thin = daily_walk(WARMUP_START_MS, N, "1", 6, vol=0.03, volume="15000000")
    r = assess("THINUSDT", thin, BTC, AppConfig())
    assert not r.passed and any("volume" in x for x in r.failures)


def test_late_listings_are_out():
    late = daily_walk(LISTED_BY_MS + DAY, 1000, "1", 7)
    r = assess("LATEUSDT", late, BTC, AppConfig())
    assert not r.passed and "listed after" in r.failures[0]


def test_screen_never_reads_the_holdout():
    late_data = daily_walk(HOLDOUT_START_MS - 400 * DAY, 800, "1", 8)
    win = universe._window(late_data)
    assert all(c.close_time < HOLDOUT_START_MS for c in win)


def test_pick_keeps_the_4_most_traded_passing_coins():
    rs = [CoinResult(f"C{i}USDT", median_volume=Dec(i)) for i in range(12)]
    rs.append(CoinResult("BADUSDT", median_volume=Dec(1000), failures=["x"]))
    assert [r.symbol for r in pick(rs)] == ["C11USDT", "C10USDT", "C9USDT", "C8USDT"]
    assert universe.MAX_EXTRA_COINS == 4


def test_memory_estimate_and_reading(tmp_path):
    assert universe.memory_needed_mb(2) == 75 and universe.memory_needed_mb(6) == 131
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:  480000 kB\nMemFree: 10000 kB\nMemAvailable:  209920 kB\n")
    assert universe.available_memory_mb(f) == 205
    assert universe.available_memory_mb(tmp_path / "missing") is None


def test_run_refuses_to_start_without_enough_memory(tmp_path, monkeypatch, capsys):
    from tests.backtest_data import H4, rules as r4, walk
    from bot.backtest import history
    for s in ("BTCUSDT", "ETHUSDT"):
        history.write_csv(tmp_path / f"{s}-4h.csv", walk(RESEARCH_START_MS - 10 * H4, 20, "100"))
        (tmp_path / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(r4((s,))[s])))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 60)
    rargs = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                               strategy=["H2"], random_runs=1, reports_dir=str(tmp_path), screened=False)
    assert cli._backtest_run(rargs, out=io.StringIO()) == 2
    assert "only 60 MB is free" in capsys.readouterr().err


def test_helpers():
    assert max_drawdown([Dec(10), Dec(12), Dec(6), Dec(13)]) == Dec("0.5")
    a = [Dec(x) for x in range(40)]
    assert correlation(a, a) == 1 and correlation(a, [-x for x in a]) == -1
    assert correlation(a[:10], a[:10]) is None


# ---------------------------------------------------------------- R-08 groups in the simulator
def test_measured_groups_let_an_uncorrelated_coin_trade_when_majors_are_full():
    cfg = AppConfig(universe=["BTCUSDT", "ETHUSDT", "LONUSDT"])
    market = FakeMarket(("BTCUSDT", "ETHUSDT", "LONUSDT"))
    market.set_bid("LONUSDT", "199.99", spread="0.01")
    market.avg["LONUSDT"] = Dec("200")
    plan = [("BTCUSDT", "80000", "78000", "86000"), ("ETHUSDT", "2500", "2400", "2750"),
            ("LONUSDT", "200", "190", "220")]

    def results(corr_groups):
        sim = SimExchange(cfg, market, clock=Clock(), corr_groups=corr_groups)
        sim.resume("sultan", "test of correlation groups")
        out = []
        for s, e, st, tp in plan:
            out.append(sim.submit(TradeIntent(s, "BUY", Dec(e), Dec(st), Dec(tp), "T", 72, "test")))
        return out
    one_group = results(None)                                              # unknown coins join majors
    assert one_group[2].decision.code is RejectCode.CORRELATION
    measured = results({"BTCUSDT": "majors", "ETHUSDT": "majors", "LONUSDT": "LONUSDT"})
    assert measured[2].filled


# ---------------------------------------------------------------- commands
class ScreenVenue:
    """all_symbol_rules + daily and 4h klines from made-up data."""
    def __init__(self, daily, h4=None):
        self.daily, self.h4 = daily, h4 or {}

    def all_symbol_rules(self):
        return [rules_for(s) for s in self.daily] + [rules_for("USDCUSDT")]

    def symbol_rules(self, symbols):
        return {s: rules_for(s) for s in symbols}

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        cs = (self.daily if interval == "1d" else self.h4)[symbol]
        sel = [c for c in cs if (start_ms is None or c.open_time >= start_ms) and (end_ms is None or c.open_time <= end_ms)]
        return sel[:limit]


def test_screen_command_writes_the_universe(tmp_path):
    daily = {"BTCUSDT": BTC, "ETHUSDT": daily_walk(WARMUP_START_MS, N, "130", 9, vol=0.01, base=BTC_RETURNS),
             "FOLUSDT": daily_walk(WARMUP_START_MS, N, "0.5", 2, vol=0.005, base=BTC_RETURNS),
             "WILDUSDT": daily_walk(WARMUP_START_MS, N, "1", 4, vol=0.09),
             "LATEUSDT": daily_walk(LISTED_BY_MS + DAY, 900, "1", 7)}
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h")
    out = io.StringIO()
    assert cli._backtest_screen(args, make_exchange=lambda **k: ScreenVenue(daily), out=out) == 0
    u = json.loads((tmp_path / "screen" / "universe.json").read_text())
    assert u["symbols"] == ["BTCUSDT", "ETHUSDT", "FOLUSDT"] and u["groups"]["FOLUSDT"] == "majors"
    text = out.getvalue()
    assert "Universe for the H2 re-test: BTCUSDT, ETHUSDT, FOLUSDT" in text and "ETHUSDT (reference)" in text
    assert (tmp_path / "screen" / "screen.md").exists()
    saved = json.loads((tmp_path / "screen" / "universe.json").read_text())["results"]
    assert {r["symbol"]: r["passed"] for r in saved} == {"FOLUSDT": True, "LATEUSDT": False, "WILDUSDT": False}
    for f in (tmp_path / "screen").glob("*-1d.csv"):                       # holdout never downloaded
        last = f.read_text().strip().splitlines()[-1]
        assert int(last.split(",")[6]) < HOLDOUT_START_MS


def test_screened_fetch_and_run(tmp_path, monkeypatch):
    from tests.backtest_data import H4, walk
    from tests.unit.test_walkforward_selection import SHORT

    (tmp_path / "screen").mkdir()
    (tmp_path / "screen" / "universe.json").write_text(json.dumps(
        {"symbols": ["BTCUSDT", "ETHUSDT", "LONUSDT"],
         "groups": {"BTCUSDT": "majors", "ETHUSDT": "majors", "LONUSDT": "LONUSDT"}}))
    n = 300 + 6 * 152
    h4 = {"BTCUSDT": walk(RESEARCH_START_MS - 300 * H4, n, "7000", 21, vol=0.015),
          "ETHUSDT": walk(RESEARCH_START_MS - 300 * H4, n, "130", 22, vol=0.015),
          "LONUSDT": walk(RESEARCH_START_MS - 300 * H4, n, "1", 23, vol=0.015)}
    venue = ScreenVenue({}, h4)
    fargs = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h", symbols=None,
                               screened=True)
    assert cli._backtest_fetch(fargs, make_exchange=lambda **k: venue) == 0
    assert (tmp_path / "LONUSDT-4h.csv").exists()
    monkeypatch.setattr(walkforward, "windows", lambda: SHORT)
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)     # independent of this machine
    rargs = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                               strategy=["H2"], random_runs=1, reports_dir=str(tmp_path / "rep"), screened=True)
    out = io.StringIO()
    assert cli._backtest_run(rargs, out=out) == 0
    text = out.getvalue()
    assert "Coins (correlation group): BTCUSDT (majors); ETHUSDT (majors); LONUSDT (LONUSDT)" in text
    assert "| LONUSDT |" in text                                            # per-coin result for the new coin


def test_screened_run_without_a_screen_explains(tmp_path, capsys):
    rargs = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                               strategy=["H2"], random_runs=1, reports_dir=str(tmp_path), screened=True)
    assert cli._backtest_run(rargs, out=io.StringIO()) == 2
    assert "bot backtest screen" in capsys.readouterr().err
