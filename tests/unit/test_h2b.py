"""H2b (docs/h2b-plan.md): 2-of-3 momentum lookbacks, volatility pause, no look-ahead, never on the holdout."""
import argparse
import io
import random
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.analysis.indicators import realized_vol
from bot.analysis.resample import DAY_MS
from bot.backtest import holdout, walkforward
from bot.backtest.engine import run
from bot.backtest.periods import RESEARCH_START_MS
from bot.core.config import AppConfig
from bot.exchange.interface import Candle
from bot.strategies.base import MAX_TUNABLE, TAKE_PROFIT_R, Exit, OpenTrade, scaled
from bot.strategies.h2b_blend import PAUSE_DAYS, VOL_DAYS, BlendedMomentum
from tests.backtest_data import H4, rules, walk
from tests.unit.test_strategies import Recorder, Source, series

SYMS = ["BTCUSDT", "ETHUSDT"]


def days_from(closes, start=RESEARCH_START_MS):
    out = []
    for i, c in enumerate(closes):
        c = Dec(str(c))
        o = Dec(str(closes[i - 1])) if i else c
        t = start + i * DAY_MS
        out.append(Candle(t, o, max(o, c) * Dec("1.002"), min(o, c) * Dec("0.998"), c, Dec(1), t + DAY_MS - 1,
                          Dec("50000000"), True))
    return out


def path(segments, seed=7):
    """Daily closes from (days, drift per day, noise) segments."""
    rng, p, out = random.Random(seed), 100.0, []
    for n, drift, noise in segments:
        for _ in range(n):
            p *= 1 + drift + rng.gauss(0, noise)
            out.append(p)
    return out


CALM_RISE = [(380, 0.0, 0.02), (60, 0.006, 0.008)]        # a noisy year, then a calm steady rise


# ---------------------------------------------------------------- contract
def test_the_definition_matches_the_frozen_plan():
    """docs/h2b-plan.md section 2, approved by Sultan. Changing any of these is a new strategy."""
    from bot.strategies import h2b_blend as h
    assert (h.LOOKBACK_DAYS, h.AGREE, h.VOL_DAYS, h.PAUSE_DAYS, h.PAUSE_PERCENTILE) == ((14, 28, 56), 2, 30, 365, Dec("0.8"))
    assert (h.ATR_N, h.HORIZON_HOURS, TAKE_PROFIT_R) == (14, 720, 3)
    s = BlendedMomentum(SYMS)
    assert (s.name, s.interval, s.threshold, s.atr_mult) == ("H2b", "4h", Dec("0.5"), Dec("3"))


def test_two_tunable_settings_and_copies_reset_caches():
    s = BlendedMomentum(SYMS)
    assert s.params() == {"threshold": Dec("0.5"), "atr_mult": Dec("3")} and len(s.params()) <= MAX_TUNABLE
    for k, v in s.params().items():
        moved = s.with_params(**{k: scaled(v, Dec("1.2"))})
        assert moved.params()[k] != v and s.params()[k] == v
    s._vol_cache["x"] = 1
    assert s.with_params(threshold=Dec(1))._vol_cache == {}
    assert len(walkforward.variants(s)) == 9
    assert s.history_needed() >= (VOL_DAYS + PAUSE_DAYS + 1) * 6


# ---------------------------------------------------------------- the entry rule
def test_enters_when_lookbacks_agree_in_calm_markets():
    s = BlendedMomentum(["BTCUSDT"])
    assert s.signal(days_from(path(CALM_RISE))) == (True, "")


def test_volatility_pause_blocks_a_strong_rise():
    s = BlendedMomentum(["BTCUSDT"])
    wild = days_from(path([(380, 0.0, 0.01), (30, 0.03, 0.05)]))
    assert s.signal(wild) == (False, "volatility pause")
    loose = s.with_params(threshold=Dec("0.01"))               # still paused: the pause does not depend on settings
    assert loose.signal(wild) == (False, "volatility pause")


def test_two_of_three_lookbacks_are_enough_one_is_not():
    s = BlendedMomentum(["BTCUSDT"])
    dip = days_from(path(CALM_RISE + [(14, -0.003, 0.004)]))    # 14-day return negative; 28 and 56 days up
    assert s.signal(dip) == (True, "")
    long_only = days_from(path(CALM_RISE + [(28, -0.004, 0.004)]))   # only the 56-day return is up
    assert s.signal(long_only) == (False, "1 of 3 lookbacks strong")


def test_needs_a_full_year_of_volatility_history():
    s = BlendedMomentum(["BTCUSDT"])
    full = days_from(path(CALM_RISE))
    short = full[-(VOL_DAYS + PAUSE_DAYS):]                   # one day short
    assert s.signal(short) == (False, "not enough history")
    assert s.signal(full[-(VOL_DAYS + PAUSE_DAYS + 1):])[0]


def test_pause_compares_today_with_the_365_days_before(monkeypatch):
    """Earlier vols 0.001..0.365: their 80th percentile is 0.2922. Today at 0.2921 may enter, 0.2922 may not."""
    days = days_from(path(CALM_RISE))
    s = BlendedMomentum(["BTCUSDT"], threshold=Dec("0.0001"))
    earlier = [Dec(k) / 1000 for k in range(1, PAUSE_DAYS + 1)]
    for today, expected in (("0.2921", (True, "")), ("0.2922", (False, "volatility pause"))):
        def fake(self, d, i, today=Dec(today)):
            return today if i == len(d) - 1 else earlier[i - (len(d) - 1 - PAUSE_DAYS)]
        monkeypatch.setattr(BlendedMomentum, "_vol", fake)
        assert s.signal(days) == expected, today


def test_cached_volatility_equals_the_plain_calculation():
    days = days_from(path(CALM_RISE))
    plain = realized_vol([d.close for d in days], VOL_DAYS)
    s = BlendedMomentum(["BTCUSDT"])
    for i in (VOL_DAYS - 1, VOL_DAYS, 200, len(days) - 1):
        assert s._vol(days, i) == plain[i]
    assert s._vol(days, len(days) - 1) == plain[-1]          # second call: from the cache
    other = days_from(path(CALM_RISE, seed=8))               # same dates, other prices: no stale cache hits
    assert [s._vol(other, i) for i in (200, len(other) - 1)] == \
        [realized_vol([d.close for d in other], VOL_DAYS)[i] for i in (200, len(other) - 1)]


# ---------------------------------------------------------------- propose / manage on 4h candles
def four_hourly(daily_segments, seed=3):
    per_4h = [(n * 6, d / 6, z / 6 ** 0.5) for n, d, z in daily_segments]
    return series(path(per_4h, seed), start=RESEARCH_START_MS)


def at_day_close(cs):
    k = max(i for i, c in enumerate(cs) if (c.close_time + 1) % DAY_MS == 0)
    return cs[:k + 1], (cs[k].close_time + 1) / 1000


def test_proposes_once_a_day_with_the_usual_exits():
    cs, midnight = at_day_close(four_hourly(CALM_RISE))
    s = BlendedMomentum(["BTCUSDT"])
    src = Source({"BTCUSDT": cs})
    (i,) = s.propose(src, midnight)
    assert i.strategy == "H2b" and i.horizon_hours == 720 and i.stop_price < i.entry_price
    assert i.take_profit_price - i.entry_price == TAKE_PROFIT_R * (i.entry_price - i.stop_price)
    assert s.propose(src, midnight + 4 * 3600) == []           # not a new day


def test_exits_when_two_of_three_returns_turn_negative():
    s = BlendedMomentum(["BTCUSDT"])
    t = OpenTrade("BTCUSDT", Dec("50"), Dec("40"), 0)
    turned, now = at_day_close(four_hourly([(100, 0.006, 0.004), (30, -0.006, 0.004)]))
    (e,) = s.manage(Source({"BTCUSDT": turned}), now, [t])
    assert isinstance(e, Exit)
    assert s.manage(Source({"BTCUSDT": turned}), now + 4 * 3600, [t]) == []    # once a day
    dip, now = at_day_close(four_hourly([(100, 0.006, 0.004), (12, -0.006, 0.004)]))
    days = s._daily(dip)
    assert [days[-1].close < days[-1 - n].close for n in (14, 28, 56)] == [True, False, False]
    assert s.manage(Source({"BTCUSDT": dip}), now, [t]) == []    # only the 14-day return is down


# ---------------------------------------------------------------- no look-ahead, end to end
def test_future_prices_never_change_past_decisions():
    n = 2700                                                   # 450 days: about 50 days of decisions
    start = RESEARCH_START_MS - 400 * H4
    full = {"BTCUSDT": walk(start, n, "7000", 11, vol=0.02, drift=0.0003),
            "ETHUSDT": walk(start, n, "130", 12, vol=0.02, drift=0.0003)}
    whole = Recorder(BlendedMomentum(SYMS))
    run(whole, full, rules(), AppConfig(), edge=True)
    decided = 0
    for k in (2500, 2650):
        cut_at = full["BTCUSDT"][k].open_time
        cut = {sym: [c for c in v if c.close_time < cut_at] for sym, v in full.items()}
        part = Recorder(BlendedMomentum(SYMS))
        run(part, cut, rules(), AppConfig(), edge=True)
        upto = [e for e in whole.log if e[0] * 1000 <= cut_at]
        assert part.log == upto, f"decisions differ before {k}"
        decided += sum(1 for e in upto if e[2])
    assert decided >= 4, "the comparison must cover real decisions"


# ---------------------------------------------------------------- the CLI, and never the holdout
def test_backtest_run_offers_h2b_and_the_holdout_refuses_it(tmp_path, capsys):
    assert isinstance(cli._make_strategy("H2b", SYMS), BlendedMomentum)
    assert "H2b" in cli.STRATEGIES and "H2b" not in cli.HOLDOUT_STRATEGIES
    with pytest.raises(SystemExit):
        cli.main(["backtest", "holdout", "--strategy", "H2b", "--operator", "Sultan", "--confirm", "x"])
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h", strategy="H2b",
                              operator="Sultan", confirm=holdout.CONFIRM_PHRASE, screened=False,
                              reports_dir=str(tmp_path / "rep"))
    assert cli._backtest_holdout(args, out=io.StringIO()) == 2
    assert "never runs on the holdout" in capsys.readouterr().err
    assert holdout.read_log(tmp_path) == []                    # nothing opened, nothing recorded
