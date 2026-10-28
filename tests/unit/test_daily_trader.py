"""H1-DailyTrader (`bot.strategies.daily_trader`): breakout, continuation and range setups on 1h candles with
4h context. Made-up candles; nothing here reads Binance or trades."""
import ast
import math
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot.analysis.resample import DAY_MS
from bot.exchange.interface import Candle
from bot.risk.models import TradeIntent
from bot.strategies import daily_trader as dt
from bot.strategies.base import MAX_TUNABLE, entry_limit
from bot.strategies.random_entry import RandomEntry

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
H1 = 3_600_000
START = 19_000 * DAY_MS
N = 400


def candles(closes, half_range):
    h, out = Dec(str(half_range)), []
    for i, c in enumerate(closes):
        c = Dec(str(c)).quantize(Dec("0.01"))
        o = out[-1].close if out else c
        t = START + i * H1
        out.append(Candle(t, o, (max(o, c) * (1 + h)).quantize(Dec("0.01")),
                          (min(o, c) * (1 - h)).quantize(Dec("0.01")), c, Dec(1), t + H1 - 1, Dec("5e7"), True))
    return out


def rising(n=N, step=0.0005, end=80_000.0):
    p0 = end / (1 + step) ** n
    return [p0 * (1 + step) ** i for i in range(n)]


def breakout(half_range=0.003, jump=0.001):
    closes = rising()
    return candles(closes + [closes[-1] * (1 + half_range) * (1 + jump)], half_range)


def ranging(n=N, amp=0.02, period=48, half_range=0.002):
    return candles([100 * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)], half_range)


def with_dip(cs, lookback=48):
    """Append a candle that dips into the bottom quarter of the last `lookback` hours' range and closes up."""
    window = cs[-lookback:]
    lo, hi = min(c.low for c in window), max(c.high for c in window)
    t = cs[-1].open_time + H1
    o, low, close = lo + Dec("0.10"), lo + Dec("0.05"), lo + Dec("0.60")
    return cs + [Candle(t, o, close + Dec("0.05"), low, close, Dec(1), t + H1 - 1, Dec("5e7"), True)], lo, hi


class Source:
    def __init__(self, by_symbol):
        self.by_symbol = by_symbol
        self.calls = []

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.calls.append((symbol, interval, limit))
        return self.by_symbol[symbol][-limit:]


# ---------------------------------------------------------------- the contract
@pytest.mark.parametrize("setup", dt.SETUPS)
def test_at_most_three_tunable_settings_and_a_name_per_setup(setup):
    s = dt.DailyTrader(["BTCUSDT"], setup)
    assert len(s.params()) <= MAX_TUNABLE and s.name == f"H1DT-{setup}" and s.interval == "1h"
    assert s.lookback == dt.DEFAULTS[setup]
    assert s.with_params(stop_atr=Dec("2")).stop_atr == Dec("2") and s.with_params(stop_atr=Dec(2)).setup == setup


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        dt.DailyTrader(["BTCUSDT"], "PULLBACK")
    with pytest.raises(ValueError):
        dt.DailyTrader(["BTCUSDT"], "BRK", stop_atr=Dec(0))


def test_the_fixed_rules():
    assert (dt.CTX_EMA, dt.HORIZON_HOURS, dt.ATR_N, dt.CONT_BREAK, dt.RANGE_MIN_ATR, dt.RANGE_ZONE) == \
        (20, 24, 14, 3, Dec(4), Dec("0.25"))
    assert dt.DEFAULTS == {"BRK": 24, "CONT": 50, "RNG": 48}


# ---------------------------------------------------------------- breakout
def test_breakout_in_an_uptrend_gives_a_long_with_atr_stop_and_r_target():
    cs = breakout()
    s = dt.DailyTrader(["BTCUSDT"], "BRK")
    st = s.trigger(cs)
    assert st is not None and st.level == max(c.high for c in cs[-25:-1])
    assert st.entry == entry_limit(cs[-1].close) and st.stop < st.entry
    assert st.target - st.entry == s.target_r * (st.entry - st.stop)
    assert s.context(cs)[0]
    [i] = s.propose(Source({"BTCUSDT": cs}), 0)
    assert isinstance(i, TradeIntent) and i.side == "BUY" and i.horizon_hours == 24 and i.strategy == "H1DT-BRK"


def test_no_breakout_without_a_fresh_break():
    cs = breakout()
    assert dt.DailyTrader(["BTCUSDT"], "BRK").trigger(cs[:-1]) is None          # no break on the last candle
    stale = cs + [Candle(cs[-1].open_time + H1, cs[-1].close, cs[-1].close * Dec("1.004"), cs[-1].close,
                         cs[-1].close * Dec("1.003"), Dec(1), cs[-1].close_time + H1, Dec("5e7"), True)]
    assert dt.DailyTrader(["BTCUSDT"], "BRK").trigger(stale) is None            # the close before already broke


def test_no_breakout_trade_below_the_4h_ema():
    falling = candles(list(reversed(rising())), 0.003)
    c = falling[-1].close * Dec("1.02")                                         # a jump in a downtrend
    cs = falling + candles([float(falling[-1].close), float(c)], 0.003)[1:]
    cs[-1] = Candle(falling[-1].open_time + H1, cs[-1].open, cs[-1].high, cs[-1].low, cs[-1].close, Dec(1),
                    falling[-1].close_time + H1, Dec("5e7"), True)
    s = dt.DailyTrader(["BTCUSDT"], "BRK")
    assert s.trigger(cs) is not None                                            # the 1h break is there ...
    assert not s.context(cs)[0] and s.propose(Source({"BTCUSDT": cs}), 0) == []  # ... but not the 4h context


# ---------------------------------------------------------------- continuation
def test_continuation_needs_a_rising_4h_and_1h_trend():
    cs = breakout()
    s = dt.DailyTrader(["BTCUSDT"], "CONT")
    st = s.trigger(cs)
    assert st is not None and st.level == max(c.high for c in cs[-4:-1]) and s.context(cs)[0]
    falling = candles(list(reversed(rising())), 0.003)
    assert not dt.DailyTrader(["BTCUSDT"], "CONT").context(falling)[0]           # a falling 4h EMA is not a trend


# ---------------------------------------------------------------- range
def test_range_buys_a_dip_to_the_bottom_with_the_stop_below_the_low_and_the_target_inside():
    cs, lo, hi = with_dip(ranging())
    s = dt.DailyTrader(["BTCUSDT"], "RNG", stop_atr=Dec("0.5"), target_r=Dec("1.5"))
    assert s.context(cs)[0], s.context(cs)[1]
    st = s.trigger(cs)
    assert st is not None and st.level == lo and st.stop < lo and st.target <= hi


def test_range_refuses_a_target_beyond_the_range():
    cs, lo, hi = with_dip(ranging())
    assert dt.DailyTrader(["BTCUSDT"], "RNG", stop_atr=Dec("0.5"), target_r=Dec("20")).trigger(cs) is None


def test_range_is_not_traded_in_a_trend():
    cs = breakout()
    assert not dt.DailyTrader(["BTCUSDT"], "RNG").context(cs)[0]


# ---------------------------------------------------------------- shared rules
def test_only_finished_candles_through_the_source_and_not_enough_history_is_no_trade():
    cs = breakout()
    src = Source({"BTCUSDT": cs})
    s = dt.DailyTrader(["BTCUSDT"], "BRK")
    s.propose(src, 0)
    assert src.calls == [("BTCUSDT", "1h", s.history_needed())]
    assert s.trigger(cs[-50:]) is None


def test_exits_are_stop_target_and_time_only():
    s = dt.DailyTrader(["BTCUSDT"], "BRK")
    assert s.manage(Source({}), 0, []) == []
    stop, hours = s.exit_plan(breakout(), Dec(80_000))
    assert stop < 80_000 and hours == 24


def test_random_entries_use_the_same_target_rule():
    s = dt.DailyTrader(["BTCUSDT"], "BRK", target_r=Dec("2.5"))
    r = RandomEntry.with_exits_of(s, ["BTCUSDT"], seed=1)
    r.chance_per_candle = 1.0
    [i] = r.propose(Source({"BTCUSDT": breakout()}), 0)
    assert i.take_profit_price - i.entry_price == Dec("2.5") * (i.entry_price - i.stop_price)


def test_the_strategy_is_pure_and_cannot_reach_orders():
    tree = ast.parse((SRC / "strategies" / "daily_trader.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.exchange.credentials",
                "bot.strategies.h2b_blend", "bot.strategies.daily_pullback", "bot.forward", "requests", "os"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad
