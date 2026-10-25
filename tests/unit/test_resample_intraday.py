"""Intraday research, step 1 (docs/intraday-plan.md): 15-minute candles and finished-hour context."""
from dataclasses import replace
from decimal import Decimal as Dec

from bot import cli
from bot.analysis.resample import DAY_MS, HOUR_MS, daily, hourly, resample
from bot.backtest import history
from bot.backtest.periods import RESEARCH_START_MS
from bot.exchange.interface import INTERVAL_MS
from tests.backtest_data import H4, walk

M15 = 900_000


def quarter_hours(n, start=RESEARCH_START_MS, seed=3):
    return walk(start, n, "40000", seed, vol=0.002, step_ms=M15)


def test_the_15_minute_interval_exists():
    assert INTERVAL_MS["15m"] == M15 and HOUR_MS == 4 * M15


def test_an_hour_is_built_from_its_four_finished_quarters():
    cs = quarter_hours(4 * 6)
    hs = hourly(cs)
    assert len(hs) == 6
    for i, h in enumerate(hs):
        q = cs[4 * i:4 * i + 4]
        assert (h.open_time, h.close_time) == (q[0].open_time, q[0].open_time + HOUR_MS - 1)
        assert (h.open, h.close) == (q[0].open, q[-1].close)
        assert h.high == max(c.high for c in q) and h.low == min(c.low for c in q)
        assert h.volume == sum((c.volume for c in q), Dec(0)) and h.quote_volume == sum((c.quote_volume for c in q), Dec(0))
        assert h.closed


def test_the_running_hour_is_never_used():
    cs = quarter_hours(4 * 3 + 2)                                   # three hours and half of a fourth
    assert len(hourly(cs)) == 3 and hourly(cs)[-1].close_time + 1 == cs[0].open_time + 3 * HOUR_MS


def test_an_hour_missing_its_last_quarter_is_left_out_a_maintenance_gap_inside_one_is_not():
    cs = quarter_hours(4 * 3)
    no_end = cs[:7] + cs[8:]                                         # hour 2 lost its :45 candle
    assert [h.open_time for h in hourly(no_end)] == [cs[0].open_time, cs[8].open_time]
    gap_inside = cs[:5] + cs[6:]                                     # hour 2 lost its :15 candle but ran to the hour
    assert len(hourly(gap_inside)) == 3


def test_hours_never_change_when_later_candles_arrive():
    cs = quarter_hours(4 * 30 + 3)
    full = hourly(cs)
    for k in range(1, len(cs)):
        part = hourly(cs[:k])
        assert part == full[:len(part)]


def test_the_general_rule_is_the_daily_rule():
    for cs in (walk(RESEARCH_START_MS, 6 * 40 + 3, step_ms=H4), quarter_hours(96 * 5 + 17)):
        assert resample(cs, DAY_MS) == daily(cs)
    gappy = quarter_hours(96 * 3)
    gappy = gappy[:100] + gappy[140:]                                # a 10-hour hole on day 2
    assert resample(gappy, DAY_MS) == daily(gappy)


def test_15_minute_history_is_checked_like_any_other():
    cs = quarter_hours(200)
    assert history.check_candles(cs, "15m").ok
    holed = cs[:50] + cs[52:]
    res = history.check_candles(holed, "15m")
    assert res.gaps == ((cs[50].open_time, cs[52].open_time),)
    odd = cs[:10] + [replace(cs[10], close_time=cs[10].open_time + 300_000 - 1)] + cs[11:]
    assert history.check_candles(odd, "15m").irregular


def test_fetch_takes_the_15_minute_interval(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_backtest_fetch", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["backtest", "fetch", "--interval", "15m", "--symbols", "BTCUSDT"])
    assert seen["args"].interval == "15m" and seen["args"].symbols == ["BTCUSDT"]
