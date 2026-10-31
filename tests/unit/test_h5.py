"""H5 (`bot.strategies.h5_breakout`, docs/h5-plan.md): daily breakout with an order placed in advance every day,
one per coin. The fixed values are pinned here; changing one is a new strategy. Made-up candles only."""
import ast
from decimal import Decimal as Dec
from pathlib import Path

from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.backtest.engine import run
from bot.backtest.walkforward import _ms
from bot.core.config import AppConfig
from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Candle
from bot.strategies import h5_breakout as h5
from bot.strategies.base import MAX_TUNABLE, Exit, OpenTrade
from bot.strategies.random_entry import RandomEntry
from tests.conftest import exchange_info_symbol

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
H4 = 4 * 3_600_000
START = _ms(2021, 1)


def candles(closes, half_range=0.01, start=START):
    h, out = Dec(str(half_range)), []
    for i, c in enumerate(closes):
        c = Dec(str(c)).quantize(Dec("0.01"))
        o = out[-1].close if out else c
        t = start + i * H4
        out.append(Candle(t, o, (max(o, c) * (1 + h)).quantize(Dec("0.01")),
                          (min(o, c) * (1 - h)).quantize(Dec("0.01")), c, Dec(1), t + H4 - 1, Dec("5e7"), True))
    return out


def flat_then_up(days_flat=70, days_up=20, price=30_000.0):
    closes = [price * (1 + 0.002 * ((i % 6) - 3)) for i in range(days_flat * 6)]       # a range
    closes += [closes[-1] * (1.004 ** (i + 1)) for i in range(days_up * 6)]            # then a steady rise
    return candles(closes)


class Source:
    def __init__(self, by):
        self.by, self.calls = by, []

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.calls.append((symbol, interval, limit))
        return self.by[symbol][-limit:]


def at_midnight(cs):
    """`now` at the first 4h close after a midnight, with the candles finished by then."""
    for i in range(len(cs) - 1, 0, -1):
        if (cs[i].close_time + 1) % DAY_MS == 0:
            return cs[:i + 1], (cs[i].close_time + 1) / 1000
    raise AssertionError


def test_the_fixed_values_and_three_tunable_settings():
    s = h5.DailyBreakout(["BTCUSDT"])
    assert s.params() == {"entry_days": 20, "stop_atr": Dec("2.0"), "exit_days": 10} and len(s.params()) <= MAX_TUNABLE
    assert (h5.LIMIT_ABOVE_PCT, h5.ATR_DAYS, h5.TAKE_PROFIT_R, h5.HORIZON_DAYS) == (Dec("0.5"), 20, Dec(3), 30)
    assert s.name == "H5" and s.interval == "4h"


def test_the_order_is_the_20_day_high_with_its_limit_stop_target_and_expiry():
    cs, now = at_midnight(flat_then_up())
    s = h5.DailyBreakout(["BTCUSDT"])
    days = daily(cs)
    o = s.order(days, now)
    level = max(d.high for d in days[-20:])
    a = atr(days[-60:], 20)[-1]
    assert o.trigger == level and o.limit == level * Dec("1.005")
    assert o.stop == level - Dec("2.0") * a and o.target == level + 3 * (level - o.stop)
    assert o.valid_until == now + 86_400                                     # until the next 00:00 UTC


def test_one_order_per_coin_once_a_day_at_the_first_4h_close_after_midnight():
    cs, now = at_midnight(flat_then_up())
    s = h5.DailyBreakout(["BTCUSDT", "ETHUSDT"])
    src = Source({"BTCUSDT": cs, "ETHUSDT": cs})
    out = s.propose(src, now)
    assert [i.symbol for i in out] == ["BTCUSDT", "ETHUSDT"]
    assert all(i.trigger_price is not None and i.valid_until == now + 86_400 and i.horizon_hours == 720 for i in out)
    assert src.calls == [("BTCUSDT", "4h", s.history_needed()), ("ETHUSDT", "4h", s.history_needed())]
    assert s.propose(src, now + 4 * 3600) == []                              # not again later that day


def test_no_order_without_enough_history():
    cs, now = at_midnight(flat_then_up(days_flat=30, days_up=5))
    assert h5.DailyBreakout(["BTCUSDT"]).order(daily(cs), now) is None


def test_exit_on_a_daily_close_below_the_10_day_low_checked_once_a_day():
    closes = [30_000.0] * (40 * 6) + [27_000.0] * 6                         # a sharp drop on the last day
    cs, now = at_midnight(candles(closes, 0.002))
    s = h5.DailyBreakout(["BTCUSDT"])
    trade = [OpenTrade("BTCUSDT", Dec(30000), Dec(28000), 0)]
    [a] = s.manage(Source({"BTCUSDT": cs}), now, trade)
    assert isinstance(a, Exit) and "10-day low" in a.reason
    assert s.manage(Source({"BTCUSDT": cs}), now + 4 * 3600, trade) == []
    calm, now2 = at_midnight(candles([30_000.0] * (40 * 6), 0.002))
    assert s.manage(Source({"BTCUSDT": calm}), now2, trade) == []


def test_random_entries_use_h5s_stop_target_and_horizon():
    s = h5.DailyBreakout(["BTCUSDT"])
    cs, now = at_midnight(flat_then_up())
    r = RandomEntry.with_exits_of(s, ["BTCUSDT"], seed=1)
    r.chance_per_candle = 1.0
    [i] = r.propose(Source({"BTCUSDT": cs}), now)
    assert i.trigger_price is None and i.horizon_hours == 720
    assert i.take_profit_price - i.entry_price == 3 * (i.entry_price - i.stop_price)


def test_a_backtest_fills_an_advance_order_on_the_breakout_and_reports_it():
    cs = flat_then_up(days_flat=70, days_up=25)
    rules = {"BTCUSDT": parse_symbol(exchange_info_symbol("BTCUSDT"))}
    res = run(h5.DailyBreakout(["BTCUSDT"]), {"BTCUSDT": cs}, rules, AppConfig(), start_ms=START)
    assert res.outcomes["advance order placed"] > 0 and res.outcomes["advance order filled"] >= 1
    assert res.outcomes["advance order expired"] > 0                         # the range days: no breakout
    assert res.fills and res.fills[0].kind == "ENTRY"
    entry = res.fills[0]
    day_start = int(entry.at * 1000) // DAY_MS * DAY_MS
    days_before = [d for d in daily(cs) if d.open_time < day_start]
    assert entry.price > max(d.high for d in days_before[-20:])              # bought only above the level


def test_h5_is_pure_and_cannot_reach_orders_or_h2b():
    tree = ast.parse((SRC / "strategies" / "h5_breakout.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.strategies.h2b_blend",
                "bot.forward", "bot.paper"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad


def test_the_level_looks_back_the_full_20_days_not_only_the_recent_ones():
    closes = [30_000.0] * (60 * 6) + [33_000.0] * 6 + [30_500.0] * (10 * 6)   # a high 10 days ago, then lower
    cs, now = at_midnight(candles(closes, 0.002))
    o = h5.DailyBreakout(["BTCUSDT"]).order(daily(cs), now)
    recent = max(d.high for d in daily(cs)[-5:])
    assert o.trigger > recent and o.trigger == max(d.high for d in daily(cs)[-20:])
