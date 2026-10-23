"""Module 4: history download and checks, the holdout lock, and the replay venue."""
import json
from dataclasses import replace
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot.backtest import history
from bot.backtest.periods import HOLDOUT_START_MS, HoldoutLocked, WARMUP_START_MS, assert_no_holdout, research_only
from bot.backtest.replay import CostModel, ReplayMarket
from bot.exchange.binance import BinanceReadOnly
from bot.exchange.interface import SpotReadVenue
from tests.backtest_data import H4, rules, walk
from tests.unit.test_binance_readonly import PUBLIC, FakeClient

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "binance"


class HistoryVenue:
    """Serves stored candles page by page, like GET /api/v3/klines."""

    def __init__(self, candles, max_page=1000):
        self.candles, self.max_page, self.calls = candles, max_page, []

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.calls.append((start_ms, end_ms, limit))
        sel = [c for c in self.candles if (start_ms is None or c.open_time >= start_ms)
               and (end_ms is None or c.open_time <= end_ms)]
        return sel[:min(limit, self.max_page)]

    def symbol_rules(self, symbols):
        return {s: rules((s,))[s] for s in symbols}


# ---------------------------------------------------------------- checks
def test_real_binance_klines_pass_the_checks():
    client = FakeClient(PUBLIC)
    ex = BinanceReadOnly(public_client=client, clock=lambda: 1792667463.9, _sleep=lambda s: None)
    candles = [c for c in ex.klines("BTCUSDT", "1d") if c.closed]
    res = history.check_candles(candles, "1d")
    assert res.ok and res.count == len(candles) > 0


@pytest.mark.parametrize("change, expected", [
    (dict(high=Dec("1")), "high/low"),
    (dict(low=Dec("999999")), "high/low"),
    (dict(open=Dec("0")), "not positive"),
    (dict(close_time=123), "closes before it opens"),
])
def test_bad_prints_are_found(change, expected):
    cs = walk(WARMUP_START_MS, 10)
    cs[5] = replace(cs[5], **change)
    res = history.check_candles(cs, "4h")
    assert not res.ok and expected in res.problems[0]


def test_duplicates_and_gaps():
    cs = walk(WARMUP_START_MS, 10)
    assert not history.check_candles(cs[:5] + [cs[4]] + cs[5:], "4h").ok
    res = history.check_candles(cs[:4] + cs[7:], "4h")
    assert res.ok and res.gaps == ((cs[4].open_time, cs[7].open_time),)        # 3 candles = 12 hours missing
    assert "1 gaps (12.0 hours without candles)" in history.describe(res, "4h")


def test_irregular_maintenance_candles_are_kept_and_overlaps_refused():
    """Binance publishes shorter/longer candles around maintenance halts (e.g. 2019-03-12)."""
    cs = walk(WARMUP_START_MS, 10)
    short = replace(cs[4], close_time=cs[4].open_time + 2 * 3_600_000 - 1)          # halted after 2 hours
    res = history.check_candles(cs[:4] + [short] + cs[5:], "4h")
    assert res.ok and res.irregular == ((short.open_time, short.close_time),)
    assert res.gaps == ((short.close_time + 1, cs[5].open_time),)                   # 2 hours without candles
    assert "1 irregular-length candles (kept)" in history.describe(res, "4h")
    long = replace(cs[4], close_time=cs[5].open_time + 3_600_000)                  # runs into the next one
    bad = history.check_candles(cs[:4] + [long] + cs[5:], "4h")
    assert not bad.ok and "overlaps" in bad.problems[0]


# ---------------------------------------------------------------- download and cache
def test_fetch_pages_through_history_and_saves(tmp_path):
    cs = walk(WARMUP_START_MS, 2500)
    venue = HistoryVenue(cs)
    res = history.fetch(venue, tmp_path, "BTCUSDT", "4h", WARMUP_START_MS, cs[-1].close_time + 1)
    assert res.ok and res.count == 2500 and len(venue.calls) == 3
    assert history.read_csv(tmp_path / "BTCUSDT-4h.csv") == cs
    assert json.loads((tmp_path / "BTCUSDT-rules.json").read_text())["symbol"] == "BTCUSDT"
    assert history.load_rules(tmp_path, "BTCUSDT") == rules(("BTCUSDT",))["BTCUSDT"]


def test_fetch_extends_an_existing_file(tmp_path):
    cs = walk(WARMUP_START_MS, 1200)
    history.fetch(HistoryVenue(cs[:700]), tmp_path, "BTCUSDT", "4h", WARMUP_START_MS, cs[-1].close_time + 1)
    venue = HistoryVenue(cs)
    history.fetch(venue, tmp_path, "BTCUSDT", "4h", WARMUP_START_MS, cs[-1].close_time + 1)
    assert venue.calls[0][0] == cs[700].open_time
    assert history.read_csv(tmp_path / "BTCUSDT-4h.csv") == cs


def test_bad_data_is_never_saved(tmp_path):
    cs = walk(WARMUP_START_MS, 50)
    cs[20] = replace(cs[20], low=Dec("999999"))
    with pytest.raises(history.DataError):
        history.fetch(HistoryVenue(cs), tmp_path, "BTCUSDT", "4h", WARMUP_START_MS, cs[-1].close_time + 1)
    assert not (tmp_path / "BTCUSDT-4h.csv").exists()


def test_unfinished_candles_are_not_stored(tmp_path):
    cs = walk(WARMUP_START_MS, 10)
    cs[-1] = replace(cs[-1], closed=False)
    history.fetch(HistoryVenue(cs), tmp_path, "BTCUSDT", "4h", WARMUP_START_MS, cs[-1].close_time + 1)
    assert len(history.read_csv(tmp_path / "BTCUSDT-4h.csv")) == 9


# ---------------------------------------------------------------- holdout lock
def test_load_never_returns_holdout_candles(tmp_path):
    start = HOLDOUT_START_MS - 10 * H4
    cs = walk(start, 30)
    history.write_csv(tmp_path / "BTCUSDT-4h.csv", cs)
    loaded = history.load(tmp_path, "BTCUSDT")
    assert len(loaded) == 10 and loaded[-1].close_time < HOLDOUT_START_MS
    assert research_only(cs) == loaded
    with pytest.raises(HoldoutLocked):
        assert_no_holdout(cs)


def test_load_refuses_missing_or_damaged_files(tmp_path):
    with pytest.raises(history.DataError, match="bot backtest fetch"):
        history.load(tmp_path, "BTCUSDT")
    cs = walk(WARMUP_START_MS, 10)
    cs[3] = replace(cs[3], high=Dec("1"))
    history.write_csv(tmp_path / "BTCUSDT-4h.csv", cs)
    with pytest.raises(history.DataError, match="damaged"):
        history.load(tmp_path, "BTCUSDT")


# ---------------------------------------------------------------- replay venue
def test_replay_shows_only_finished_candles_and_the_open_price():
    cs = walk(WARMUP_START_MS, 20)
    m = ReplayMarket({"BTCUSDT": cs}, rules(("BTCUSDT",)), CostModel(), "4h")
    assert isinstance(m, SpotReadVenue)
    m.set_time(cs[10].open_time)
    seen = m.klines("BTCUSDT", "4h")
    assert seen == cs[:10] and all(c.close_time < m.now_ms for c in seen)
    b = m.books(["BTCUSDT"])["BTCUSDT"]
    assert b.bid == cs[10].open * Dec("0.9999") and b.ask == cs[10].open * Dec("1.0001")
    assert m.quote_volume_24h(["BTCUSDT"])["BTCUSDT"] == 6 * Dec("50000000")
    assert m.klines("BTCUSDT", "4h", limit=3) == cs[7:10]
    assert m.klines("BTCUSDT", "4h", start_ms=cs[2].open_time, limit=3) == cs[2:5]   # forward from start, as Binance
    with pytest.raises(ValueError):
        m.klines("BTCUSDT", "1d")


def test_doubled_costs():
    c = CostModel().doubled()
    assert c.taker_fee == Dec("0.002") and c.half_spread_pct == Dec("0.02") and c.slippage_multiplier == 2
