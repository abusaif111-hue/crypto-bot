"""Daily market observations (docs/intel-plan.md, step 2): finished candles only, nothing filled in."""
import argparse
import ast
import io
import json
from dataclasses import replace
from decimal import Decimal as Dec
from pathlib import Path
from statistics import mean

from bot import cli
from bot.analysis import regime
from bot.analysis.indicators import realized_vol
from bot.analysis.resample import daily
from bot.backtest import selection
from bot.exchange.interface import Book
from bot.intel import market
from tests.unit.test_h2b import path
from tests.unit.test_signal_report import four_hourly

SRC = Path(__file__).resolve().parents[2] / "src" / "bot" / "intel" / "market.py"
RISE = [(380, 0.0, 0.02), (60, 0.004, 0.01)]


def test_every_figure_is_worked_out_from_the_finished_days():
    cs = four_hourly(path(RISE))
    o = market.observe("BTCUSDT", cs, Dec("100"), Dec("100.02"))
    closes = [d.close for d in daily(cs)]
    assert o.close == closes[-1] and o.day is not None
    assert o.change_1d_pct == ((closes[-1] / closes[-2] - 1) * 100).quantize(Dec("0.01"))
    assert o.change_7d_pct == ((closes[-1] / closes[-8] - 1) * 100).quantize(Dec("0.01"))
    avg = mean(closes[-50:])
    assert o.above_avg50 == (closes[-1] > avg)
    assert abs(o.from_avg50_pct - (closes[-1] / avg - 1) * 100) < Dec("0.01")
    assert o.avg50_rising == (mean(closes[-50:]) > mean(closes[-60:-10]))
    vols = realized_vol(closes, 30)
    assert o.vol30_pct == (vols[-1] * 100).quantize(Dec("0.01"))
    earlier = vols[-366:-1]
    assert o.vol_rank_pct == (Dec(sum(1 for v in earlier if v < vols[-1])) / 365 * 100).quantize(Dec("0.1"))
    assert o.regime == regime.regime_labels(cs)[daily(cs)[-1].open_time][0]
    assert o.volume_vs_avg == Dec("1.00") and o.spread_pct == Dec("0.0200") and o.notes == []


def test_trend_matches_a_plain_calculation_on_many_histories():
    seen = set()
    for seed in range(120):
        closes = path([(70, 0.0, 0.02)], seed=seed)
        o = market.observe("BTCUSDT", four_hourly(closes))
        want_above = closes[-1] > mean(closes[-50:])
        want_rising = mean(closes[-50:]) > mean(closes[-60:-10])
        assert (o.above_avg50, o.avg50_rising) == (want_above, want_rising), seed
        seen.add((want_above, want_rising))
    assert len(seen) == 4                                                   # every combination was tested


def test_the_trend_uses_todays_50_day_average():
    closes = [100.0] * 60
    closes[-51] = 150.0                                     # in yesterday's 50 days, not in today's
    closes[-1] = 100.5                                      # above today's average (100.01), below yesterday's (101)
    o = market.observe("BTCUSDT", four_hourly(closes))
    assert o.above_avg50 is True and o.from_avg50_pct == Dec("0.49")


def test_the_volatility_rank_compares_today_with_the_365_days_before():
    closes = [100 * (1.005 if i % 2 else 1) for i in range(400)]             # calm zig-zag for a year
    closes += [100 * (1.05 if i % 2 else 1) for i in range(30)]              # then 30 wild days
    o = market.observe("BTCUSDT", four_hourly(closes))
    assert o.vol_rank_pct == Dec("100.0")                                    # above every earlier day, not 99.7


def test_the_regime_is_todays_label():
    closes = [100 * 0.9995 ** i for i in range(230)] + [120]              # a falling year, then a jump
    cs = four_hourly(closes)
    labels = regime.regime_labels(cs)
    days = daily(cs)
    assert labels[days[-2].open_time][0] == "bear" and labels[days[-1].open_time][0] == "sideways"
    assert market.observe("BTCUSDT", cs).regime == "sideways"


def test_the_spread_is_measured_against_the_mid_price():
    o = market.observe("BTCUSDT", four_hourly(path(RISE)), Dec("100"), Dec("110"))
    assert o.spread_pct == Dec("9.5238")


def test_a_busy_day_shows_in_the_volume_ratio():
    cs = four_hourly(path(RISE))
    busy = cs[:-6] + [replace(c, quote_volume=c.quote_volume * 3) for c in cs[-6:]]
    assert market.observe("BTCUSDT", busy).volume_vs_avg == Dec("3.00")


def test_a_running_day_is_never_used():
    cs = four_hourly(path(RISE))
    o = market.observe("BTCUSDT", cs + four_hourly([999])[:2])          # two 4h candles of an unfinished day
    assert o.close == market.observe("BTCUSDT", cs).close


def test_short_history_is_reported_not_filled_in():
    o = market.observe("BTCUSDT", four_hourly(path([(40, 0.0, 0.01)])))
    assert o.above_avg50 is None and o.vol_rank_pct is None and o.regime == "unlabelled"
    assert any("no trend" in n for n in o.notes) and any("no rank" in n for n in o.notes)
    assert market.observe("BTCUSDT", four_hourly([100])).notes == ["not enough finished days"]
    half = market.observe("BTCUSDT", four_hourly(path([(200, 0.0, 0.01)])))
    assert half.vol30_pct is not None and half.vol_rank_pct is None             # 200 days: no year to rank against


def test_breadth_counts_only_coins_with_a_trend():
    up = market.observe("BTCUSDT", four_hourly(path(RISE)))
    down = market.observe("ETHUSDT", four_hourly(path([(380, 0.0, 0.02), (60, -0.004, 0.01)])))
    short = market.observe("XRPUSDT", four_hourly(path([(20, 0.0, 0.01)])))
    assert up.above_avg50 and not down.above_avg50
    assert market.breadth([up, down, short]) == market.Breadth(above_avg50=1, known=2, total=3)
    text = market.render([up, down, short], 1_792_830_000)
    assert "Breadth: 1 of 2 coins above their 50-day average (1 without enough history)." in text
    assert "not forecasts" in text and "note: fewer than 50 days" in text


class FakeData:
    def __init__(self, candles, fail=(), books_fail=False):
        self.candles, self.fail, self.books_fail = candles, fail, books_fail

    def books(self, symbols):
        if self.books_fail:
            raise TimeoutError("book timed out")
        return {s: Book(Dec("100"), Dec("100.01")) for s in symbols}

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        if symbol in self.fail:
            raise ConnectionError("no route")
        assert interval == "4h" and limit == market.CANDLES_NEEDED
        return self.candles[-limit:]


def test_failures_are_shown_never_a_crash():
    cs = four_hourly(path(RISE))
    btc, eth = market.gather(FakeData(cs, fail=("ETHUSDT",)), ["BTCUSDT", "ETHUSDT"])
    assert btc.spread_pct is not None and eth.error == "ConnectionError: no route"
    (only,) = market.gather(FakeData(cs, books_fail=True), ["BTCUSDT"])
    assert only.spread_pct is None and only.close is not None                 # no book: no spread, rest stays
    assert "could not read prices: ConnectionError" in market.render([btc, eth], 0)


def test_the_command_uses_the_forward_tests_coins(tmp_path, monkeypatch):
    from bot.market import data as market_data
    (tmp_path / "forward-h2b").mkdir()
    (tmp_path / "forward-h2b" / "forward.json").write_text(json.dumps({"symbols": ["BTCUSDT", "XRPUSDT"]}))
    seen = {}

    def mds(venue, cfg):
        seen["universe"] = cfg.universe
        return FakeData(four_hourly(path(RISE)))
    monkeypatch.setattr(market_data, "MarketDataService", mds)
    out = io.StringIO()
    args = argparse.Namespace(config="config/default.yaml", home=str(tmp_path), symbols=None)
    assert cli._market(args, make_exchange=lambda environment: object(), out=out) == 0
    assert seen["universe"] == ["BTCUSDT", "XRPUSDT"] and "XRPUSDT" in out.getvalue()


def test_the_backtests_and_the_daily_view_share_one_regime_rule():
    assert selection.regime_labels is regime.regime_labels


def _imports(src):
    tree = ast.parse(src.read_text())
    return {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}


def test_the_market_view_cannot_trade():
    for src in (SRC, SRC.parents[1] / "analysis" / "regime.py"):
        mods = _imports(src)
        assert not any(m.startswith(("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.risk",
                                     "bot.paper", "bot.backtest", "bot.strategies", "bot.forward")) for m in mods), mods
