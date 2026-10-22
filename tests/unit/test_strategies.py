"""Module 4 step 2: strategies H1-H3, daily candles, exits, no look-ahead."""
from dataclasses import replace
from decimal import Decimal as Dec

import pytest

from bot.analysis.resample import DAY_MS, daily
from bot.backtest.engine import run
from bot.backtest.periods import RESEARCH_START_MS
from bot.core.config import AppConfig
from bot.exchange.interface import Candle
from bot.strategies.base import MAX_TUNABLE, TAKE_PROFIT_R, Exit, OpenTrade, RaiseStop, scaled
from bot.strategies.h1_trend import TrendBreakout
from bot.strategies.h2_momentum import TimeSeriesMomentum
from bot.strategies.h3_mean_reversion import RangeMeanReversion
from bot.strategies.random_entry import RandomEntry
from tests.backtest_data import H4, rules, walk

SYMS = ["BTCUSDT", "ETHUSDT"]
ALL = [TrendBreakout(SYMS), TimeSeriesMomentum(SYMS), RangeMeanReversion(SYMS)]


class Source:
    """closed_candles over a fixed list (what the strategy is allowed to see)."""
    def __init__(self, by_symbol):
        self.by_symbol = by_symbol

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        return self.by_symbol[symbol][-limit:]


def series(closes, start=RESEARCH_START_MS - 400 * H4, spread="0.002"):
    out = []
    for i, c in enumerate(closes):
        c = Dec(str(c))
        o = Dec(str(closes[i - 1])) if i else c
        hi = max(o, c) * (1 + Dec(spread))
        lo = min(o, c) * (1 - Dec(spread))
        t = start + i * H4
        out.append(Candle(t, o, hi, lo, c, Dec(1), t + H4 - 1, Dec("50000000"), True))
    return out


# ---------------------------------------------------------------- contract
@pytest.mark.parametrize("s", ALL, ids=lambda s: s.name)
def test_at_most_three_tunable_settings_and_copies(s):
    assert len(s.params()) <= MAX_TUNABLE
    for k, v in s.params().items():
        moved = s.with_params(**{k: scaled(v, Dec("1.2"))})
        assert moved.params()[k] != v and type(moved.params()[k]) is type(v)
        assert s.params()[k] == v                            # the original is unchanged


def test_scaled_keeps_whole_numbers_whole():
    assert scaled(30, Dec("0.8")) == 24 and scaled(4, Dec("1.2")) == 5 and scaled(1, Dec("0.2")) == 1
    assert scaled(Dec("2.5"), Dec("1.2")) == Dec("3.00")


def test_daily_candles_only_complete_days():
    cs = walk(RESEARCH_START_MS, 6 * 3 + 2)                 # 3 full days + 2 candles of a 4th
    days = daily(cs)
    assert len(days) == 3
    d0 = days[0]
    assert d0.open == cs[0].open and d0.close == cs[5].close and d0.high == max(c.high for c in cs[:6])
    assert d0.close_time == RESEARCH_START_MS + DAY_MS - 1
    halted = cs[:2] + [replace(cs[2], close_time=cs[2].open_time + 3_600_000 - 1)] + cs[4:6]   # maintenance day
    assert len(daily(halted)) == 1


# ---------------------------------------------------------------- H1
def test_h1_buys_a_breakout_in_an_uptrend_only():
    up = [100 + i * 0.05 for i in range(400)] + [125]          # steady rise, then a breakout close
    s = TrendBreakout(["BTCUSDT"])
    (i,) = s.propose(Source({"BTCUSDT": series(up)}), 0)
    assert i.strategy == "H1" and i.stop_price < i.entry_price < i.take_profit_price
    assert i.take_profit_price - i.entry_price == TAKE_PROFIT_R * (i.entry_price - i.stop_price)
    assert i.horizon_hours == 168
    down = [200 - i * 0.2 for i in range(400)] + [200]          # a jump in a falling market: no buy
    assert s.propose(Source({"BTCUSDT": series(down)}), 0) == []
    flat = [100 + i * 0.05 for i in range(401)]                 # no close above the prior range high
    assert s.propose(Source({"BTCUSDT": series(flat, spread="0.01")}), 0) == []


def test_h1_trails_its_stop_up_never_down():
    cs = series([100 + i * 0.05 for i in range(400)])
    s = TrendBreakout(["BTCUSDT"])
    t = OpenTrade("BTCUSDT", Dec("110"), Dec("100"), cs[-50].open_time / 1000)
    (a,) = s.manage(Source({"BTCUSDT": cs}), 0, [t])
    assert isinstance(a, RaiseStop) and a.stop > Dec("100")
    high = OpenTrade("BTCUSDT", Dec("110"), Dec("200"), cs[-50].open_time / 1000)
    assert s.manage(Source({"BTCUSDT": cs}), 0, [high]) == []


# ---------------------------------------------------------------- H2
def test_h2_acts_once_a_day_and_exits_when_momentum_turns():
    rise = [100 * (1.004 ** i) for i in range(400)]
    cs = series(rise)
    s = TimeSeriesMomentum(["BTCUSDT"])
    day_close = next(c for c in reversed(cs) if (c.close_time + 1) % DAY_MS == 0)
    k = cs.index(day_close)
    src = Source({"BTCUSDT": cs[:k + 1]})
    at_midnight = (day_close.close_time + 1) / 1000
    (i,) = s.propose(src, at_midnight)
    assert i.strategy == "H2" and i.horizon_hours == 720
    assert s.propose(src, at_midnight + 4 * 3600) == []          # not a new day: nothing
    fall = series([100 * (0.996 ** i) for i in range(400)])
    dc = next(c for c in reversed(fall) if (c.close_time + 1) % DAY_MS == 0)
    src2 = Source({"BTCUSDT": fall[:fall.index(dc) + 1]})
    t = OpenTrade("BTCUSDT", Dec("50"), Dec("40"), 0)
    assert s.propose(src2, (dc.close_time + 1) / 1000) == []
    (e,) = s.manage(src2, (dc.close_time + 1) / 1000, [t])
    assert isinstance(e, Exit)


# ---------------------------------------------------------------- H3
def test_h3_buys_oversold_dips_only_in_a_range():
    import math
    rng = [100 + 3 * math.sin(i / 6) for i in range(400)]        # sideways
    dip = rng + [rng[-1] * 0.97, rng[-1] * 0.95, rng[-1] * 0.94]
    s = RangeMeanReversion(["BTCUSDT"])
    (i,) = s.propose(Source({"BTCUSDT": series(dip)}), 0)
    assert i.strategy == "H3" and i.horizon_hours == 24
    trend = [100 * (1.003 ** i) for i in range(400)]
    tdip = trend + [trend[-1] * 0.97, trend[-1] * 0.95, trend[-1] * 0.94]
    assert s.propose(Source({"BTCUSDT": series(tdip)}), 0) == []   # dip in a strong trend: no buy
    recovered = dip + [dip[-1] * 1.05, dip[-1] * 1.08]
    (e,) = s.manage(Source({"BTCUSDT": series(recovered)}), 0, [OpenTrade("BTCUSDT", Dec(95), Dec(90), 0)])
    assert isinstance(e, Exit)


# ---------------------------------------------------------------- random with the same exits
def test_random_entries_borrow_a_strategys_exits():
    h2 = TimeSeriesMomentum(SYMS)
    r = RandomEntry(SYMS, seed=1, exits=h2)
    assert r.params()["exits_from"] == "H2" and r.history_needed() == h2.history_needed()
    with pytest.raises(TypeError):
        r.with_params(seed=2)


# ---------------------------------------------------------------- no look-ahead, end to end
class Recorder:
    """Wraps a strategy and records every decision it makes."""
    def __init__(self, inner):
        self.inner, self.log = inner, []
        self.name, self.interval, self.symbols = inner.name, inner.interval, inner.symbols

    def params(self):
        return self.inner.params()

    def propose(self, data, now):
        out = self.inner.propose(data, now)
        self.log.append((now, "propose", [(i.symbol, i.entry_price, i.stop_price, i.take_profit_price) for i in out]))
        return out

    def manage(self, data, now, open_trades):
        out = self.inner.manage(data, now, open_trades)
        self.log.append((now, "manage", out))
        return out


@pytest.mark.parametrize("s", ALL, ids=lambda s: s.name)
def test_future_prices_never_change_past_decisions(s):
    """Run on history cut at T and on the full history: every decision up to T must be identical."""
    n = 1700
    vol = 0.006 if s.name == "H3" else 0.02                      # H3 only trades in calm, sideways markets
    full = {"BTCUSDT": walk(RESEARCH_START_MS - 400 * H4, n, "7000", 11, vol=vol),
            "ETHUSDT": walk(RESEARCH_START_MS - 400 * H4, n, "130", 12, vol=vol)}
    whole = Recorder(s.with_params(**s.params()))
    run(whole, full, rules(), AppConfig(), edge=True)
    decided = 0
    for k in (900, 1100, 1300, 1500):
        cut_at = full["BTCUSDT"][k].open_time
        cut = {sym: [c for c in v if c.close_time < cut_at] for sym, v in full.items()}
        part = Recorder(s.with_params(**s.params()))
        run(part, cut, rules(), AppConfig(), edge=True)
        upto = [e for e in whole.log if e[0] * 1000 <= cut_at]
        assert part.log == upto, f"decisions differ before {k}"
        decided += sum(1 for e in upto if e[2])
    assert decided >= 4, "the comparison must cover real decisions"
