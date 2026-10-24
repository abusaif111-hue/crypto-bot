"""H4 (docs/h4-plan.md): hourly pullbacks in an uptrend, no look-ahead, its coins from one memory check."""
import argparse
import io
import json
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.analysis.indicators import atr, sma, zscore
from bot.analysis.resample import DAY_MS, daily
from bot.backtest import h4_coins, history
from bot.backtest.engine import run
from bot.backtest.periods import RESEARCH_START_MS
from bot.core.config import AppConfig
from bot.exchange.interface import Candle
from bot.strategies.base import MAX_TUNABLE, TAKE_PROFIT_R, Exit, OpenTrade, scaled
from bot.strategies.h2_momentum import TimeSeriesMomentum
from bot.strategies.h4_pullback import TrendPullback, new_day
from bot.strategies.random_entry import RandomEntry
from tests.backtest_data import rules, walk
from tests.unit.test_strategies import Recorder, Source

HOUR = 3_600_000
SYMS = ["BTCUSDT", "ETHUSDT"]
FOUR = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT"]
GROUPS = {"XRPUSDT": "XRPUSDT", "BNBUSDT": "BNBUSDT"}


def hourly(closes, start=RESEARCH_START_MS - 70 * DAY_MS, spread="0.001"):
    out = []
    for i, c in enumerate(closes):
        c = Dec(str(c))
        o = Dec(str(closes[i - 1])) if i else c
        t = start + i * HOUR
        out.append(Candle(t, o, max(o, c) * (1 + Dec(spread)), min(o, c) * (1 - Dec(spread)), c, Dec(1),
                          t + HOUR - 1, Dec("50000000"), True))
    return out


def rising(days=68, per_hour=0.0002, wiggle=0.001):
    """A steady hourly rise with a small zig-zag, so the 24h standard deviation is not zero."""
    p, out = 100.0, []
    for i in range(days * 24):
        p *= 1 + per_hour
        out.append(p * (1 + (wiggle if i % 2 else -wiggle)))
    return out


def with_drop(closes, drop=0.03):
    return closes + [closes[-1] * (1 - drop)]


def at_hour_close(closes, **kw):
    cs = hourly(closes, **kw)
    return cs, (cs[-1].close_time + 1) / 1000


# ---------------------------------------------------------------- contract
def test_the_definition_matches_the_frozen_plan():
    """docs/h4-plan.md section 3, approved by Sultan. Changing any of these is a new strategy."""
    from bot.strategies import h4_pullback as h
    assert (h.TREND_DAYS, h.SLOPE_DAYS, h.DROP_HOURS, h.ATR_N, h.HORIZON_HOURS, TAKE_PROFIT_R) == (50, 10, 24, 14, 72, 3)
    s = TrendPullback(SYMS)
    assert (s.name, s.interval, s.dip, s.stop_atr) == ("H4", "1h", Dec("2.0"), Dec("1.0"))
    assert s.history_needed() == 64 * 24


def test_two_tunable_settings_and_copies():
    s = TrendPullback(SYMS)
    assert set(s.params()) == {"dip", "stop_atr"} and len(s.params()) <= MAX_TUNABLE
    for k, v in s.params().items():
        moved = s.with_params(**{k: scaled(v, Dec("1.2"))})
        assert moved.params()[k] == v * Dec("1.2") and s.params()[k] == v


# ---------------------------------------------------------------- entries
def test_buys_the_first_hour_of_a_sharp_drop_in_an_uptrend():
    cs, now = at_hour_close(with_drop(rising()))
    s = TrendPullback(["BTCUSDT"])
    (i,) = s.propose(Source({"BTCUSDT": cs}), now)
    days = daily(cs)
    a = atr(days[-42:], 14)[-1]
    assert i.strategy == "H4" and i.horizon_hours == 72
    assert i.entry_price == cs[-1].close * Dec("1.001")
    assert i.stop_price == i.entry_price - a
    assert i.take_profit_price - i.entry_price == TAKE_PROFIT_R * (i.entry_price - i.stop_price)
    wider = s.with_params(stop_atr=Dec("1.5")).propose(Source({"BTCUSDT": cs}), now)[0]
    assert wider.stop_price == i.entry_price - Dec("1.5") * a


def test_only_the_first_hour_of_a_drop_counts():
    closes = with_drop(rising())
    cs, now = at_hour_close(closes + [closes[-1] * 0.99])          # still falling the next hour
    s = TrendPullback(["BTCUSDT"])
    z = zscore([c.close for c in cs[-25:]], 24)
    assert z[-1] < -2 and z[-2] < -2
    assert s.propose(Source({"BTCUSDT": cs}), now) == []


def test_a_small_dip_is_not_a_sharp_drop():
    cs, now = at_hour_close(with_drop(rising(), drop=0.006))
    z = zscore([c.close for c in cs[-25:]], 24)
    assert -2 < z[-1] < 0
    assert TrendPullback(["BTCUSDT"]).propose(Source({"BTCUSDT": cs}), now) == []
    looser = TrendPullback(["BTCUSDT"], dip=-z[-1] * Dec("0.9"))
    assert len(looser.propose(Source({"BTCUSDT": cs}), now)) == 1


def test_the_drop_is_measured_on_the_last_24_hourly_closes():
    s = TrendPullback(["BTCUSDT"])
    for closes in (with_drop(rising()), with_drop(rising(), 0.004), with_drop(rising(), 0.01)):
        cs = hourly(closes)
        z = zscore([c.close for c in cs[-25:]], 24)
        assert s.sharp_drop(cs) == (z[-1] < -2 and not z[-2] < -2)
    assert not s.sharp_drop(hourly(with_drop(rising()))[-24:])        # 24 hours are not enough to compare


def test_no_entry_without_an_uptrend():
    s = TrendPullback(["BTCUSDT"])
    falling = [100 * (1 - 0.0002) ** i * (1.001 if i % 2 else 0.999) for i in range(68 * 24)]
    cs, now = at_hour_close(with_drop(falling))
    assert s.sharp_drop(cs) and s.propose(Source({"BTCUSDT": cs}), now) == []
    # a sharp rally lifts the close above a 50-day average that is still lower than 10 days ago
    v = [100 * (1 - 0.0006) ** i for i in range(60 * 24)]
    v += [v[-1] * (1 + 0.004) ** i * (1.001 if i % 2 else 0.999) for i in range(1, 8 * 24 + 1)]
    cs, now = at_hour_close(with_drop(v, drop=0.25))
    days = daily(cs)
    avg = sma([d.close for d in days], 50)
    assert days[-1].close > avg[-1] and avg[-1] < avg[-11]
    assert s.sharp_drop(cs) and s.propose(Source({"BTCUSDT": cs}), now) == []


def test_not_enough_daily_history_means_no_entry():
    s = TrendPullback(["BTCUSDT"])
    cs, now = at_hour_close(with_drop(rising(days=55)))
    assert len(daily(cs)) < 60 and s.sharp_drop(cs)
    assert s.propose(Source({"BTCUSDT": cs}), now) == []


def test_the_running_day_is_not_part_of_the_trend():
    """Hours after midnight belong to a day that has not finished: the trend uses yesterday's close."""
    closes = rising()
    cs = hourly(with_drop(closes))
    assert daily(cs)[-1].close == cs[-2].close                     # the drop hour starts a new day
    assert daily(cs)[-1].close_time + 1 == cs[-1].open_time


def test_trend_and_drop_match_a_plain_calculation():
    """The uptrend and the drop, recomputed with plain averages on many random histories."""
    import random
    from statistics import mean, stdev
    s, agree = TrendPullback(["BTCUSDT"]), {"trend": set(), "drop": set()}
    for seed in range(300):
        rng = random.Random(seed)
        drift, noise = rng.uniform(-0.004, 0.004), rng.uniform(0.005, 0.03)
        closes, p = [], 100.0
        for _ in range(64):
            p *= 1 + drift + rng.gauss(0, noise)
            closes.append(p)
        days = [Candle(RESEARCH_START_MS + i * DAY_MS, Dec(str(c)), Dec(str(c)), Dec(str(c)), Dec(str(c)), Dec(1),
                       RESEARCH_START_MS + (i + 1) * DAY_MS - 1, Dec(1), True) for i, c in enumerate(closes)]
        want = closes[-1] > mean(closes[-50:]) and mean(closes[-50:]) > mean(closes[-60:-10])
        assert s.uptrend(days) == want, seed
        agree["trend"].add(want)
        hours = [p * (1 + rng.gauss(0, noise / 5)) for _ in range(30)]
        hours[-1] *= 1 - rng.uniform(0, noise / 2)
        hs = hourly(hours)

        def z(xs):
            return (xs[-1] - mean(xs)) / stdev(xs)
        want = z(hours[-24:]) < -2 and not z(hours[-25:-1]) < -2
        assert s.sharp_drop(hs) == want, seed
        agree["drop"].add(want)
    assert agree == {"trend": {True, False}, "drop": {True, False}}     # both outcomes were tested


def test_a_close_below_a_rising_average_is_not_an_uptrend():
    up = rising()
    v = up + [up[-1] * (1 - 0.003) ** i * (1.001 if i % 2 else 0.999) for i in range(1, 3 * 24 + 1)]
    cs, now = at_hour_close(with_drop(v, drop=0.05))
    days = daily(cs)
    avg = sma([d.close for d in days], 50)
    assert days[-1].close < avg[-1] and avg[-1] > avg[-11]
    s = TrendPullback(["BTCUSDT"])
    assert s.sharp_drop(cs) and s.propose(Source({"BTCUSDT": cs}), now) == []


# ---------------------------------------------------------------- exits
def test_exits_once_a_day_when_the_daily_close_falls_below_its_average():
    s = TrendPullback(["BTCUSDT"])
    t = OpenTrade("BTCUSDT", Dec("100"), Dec("95"), 0)
    up = rising()
    down = up + [up[-1] * (1 - 0.004) ** i for i in range(1, 6 * 24 + 1)]
    cs, now = at_hour_close(down)
    assert new_day(now)
    days = daily(cs)
    assert days[-1].close < sma([d.close for d in days], 50)[-1]
    (e,) = s.manage(Source({"BTCUSDT": cs}), now, [t])
    assert isinstance(e, Exit) and e.symbol == "BTCUSDT"
    later = cs + hourly([down[-1]] * 2, start=cs[-1].open_time + HOUR)
    assert not new_day((later[-1].close_time + 1) / 1000)
    assert s.manage(Source({"BTCUSDT": later}), (later[-1].close_time + 1) / 1000, [t]) == []
    cs, now = at_hour_close(up)
    assert new_day(now) and s.manage(Source({"BTCUSDT": cs}), now, [t]) == []    # still above its average


def test_new_day_is_the_first_hour_after_midnight():
    midnight = RESEARCH_START_MS / 1000
    assert new_day(midnight) and new_day(midnight + 3599) and not new_day(midnight + 3600)
    assert not new_day(midnight - 1)


# ---------------------------------------------------------------- no look-ahead, end to end
def test_future_prices_never_change_past_decisions():
    n = 24 * 110                                                # 110 days: about 45 days of hourly decisions
    start = RESEARCH_START_MS - 64 * DAY_MS
    full = {"BTCUSDT": walk(start, n, "7000", 11, vol=0.006, drift=0.0002, step_ms=HOUR),
            "ETHUSDT": walk(start, n, "130", 12, vol=0.006, drift=0.0002, step_ms=HOUR)}
    whole = Recorder(TrendPullback(SYMS))
    run(whole, full, rules(), AppConfig(), edge=True)
    decided = 0
    for k in (24 * 80, 24 * 95 + 7, 24 * 105 + 1):
        cut_at = full["BTCUSDT"][k].open_time
        cut = {sym: [c for c in v if c.close_time < cut_at] for sym, v in full.items()}
        part = Recorder(TrendPullback(SYMS))
        run(part, cut, rules(), AppConfig(), edge=True)
        upto = [e for e in whole.log if e[0] * 1000 <= cut_at]
        assert part.log == upto, f"decisions differ before {k}"
        decided += sum(1 for e in upto if e[2])
    assert decided >= 4, "the comparison must cover real decisions"


# ---------------------------------------------------------------- random entries with the same exits
def test_random_entries_use_the_strategys_candles_at_the_same_rate_per_day():
    r = RandomEntry.with_exits_of(TrendPullback(SYMS), SYMS, seed=3)
    assert r.interval == "1h" and r.chance_per_candle == pytest.approx(1 / 120) and r.exits.name == "H4"
    h2 = RandomEntry.with_exits_of(TimeSeriesMomentum(SYMS), SYMS, seed=3)
    old = RandomEntry(SYMS, seed=3, exits=TimeSeriesMomentum(SYMS))
    assert (h2.interval, h2.chance_per_candle) == (old.interval, old.chance_per_candle) == ("4h", 1 / 30)


# ---------------------------------------------------------------- which coins: the one-time memory check
def _data(tmp_path, coins=FOUR):
    for s in coins:
        f = history._csv_path(tmp_path, s, "1h")
        if not f.exists():
            f.write_text("x\n")
    return tmp_path


def _decide(tmp_path, peaks, free, **kw):
    calls = []

    def measure(data_dir, symbols, cfg, groups):
        calls.append(list(symbols))
        return peaks[len(symbols)]
    rec = h4_coins.decide(_data(tmp_path), kw.pop("four", FOUR), GROUPS, AppConfig(), kw.pop("operator", "Sultan"),
                          measure=measure, available=lambda: free, now=lambda: 0)
    return rec, calls


def test_four_coins_when_the_estimate_fits(tmp_path):
    rec, calls = _decide(tmp_path, {1: 100, 2: 150}, free=280)            # 150 + 2 x 50 = 250; + 30 = 280
    assert calls == [["BTCUSDT"], ["BTCUSDT", "ETHUSDT"]]
    assert rec["four_coins_fit"] and rec["coins"] == FOUR and rec["groups"] == GROUPS
    assert rec["estimate_4_coins_mb"] == 250 and h4_coins.needed_mb(rec) == 250
    assert json.loads((tmp_path / h4_coins.DECISION_FILE).read_text()) == rec == h4_coins.load(tmp_path)
    assert "all four coins fit" in h4_coins.render(rec)


def test_btc_and_eth_only_when_it_does_not_fit(tmp_path):
    rec, _ = _decide(tmp_path, {1: 100, 2: 150}, free=279)
    assert not rec["four_coins_fit"] and rec["coins"] == SYMS and rec["groups"] is None
    assert h4_coins.needed_mb(rec) == 150
    assert "do not fit" in h4_coins.render(rec) and "BTCUSDT, ETHUSDT" in h4_coins.render(rec)


def test_the_decision_is_made_once_and_never_overwritten(tmp_path):
    rec, _ = _decide(tmp_path, {1: 100, 2: 150}, free=100)
    with pytest.raises(h4_coins.H4CoinsError, match="already decided"):
        _decide(tmp_path, {1: 100, 2: 150}, free=10_000)
    assert h4_coins.load(tmp_path) == rec


def test_the_check_refuses_what_it_cannot_measure(tmp_path):
    with pytest.raises(h4_coins.H4CoinsError, match="no 1h history for BNBUSDT"):
        h4_coins.decide(_data(tmp_path, FOUR[:3]), FOUR, GROUPS, AppConfig(), "Sultan",
                        measure=lambda *a: 1, available=lambda: 1000)
    with pytest.raises(h4_coins.H4CoinsError, match="available memory"):
        h4_coins.decide(_data(tmp_path), FOUR, GROUPS, AppConfig(), "Sultan", measure=lambda *a: 1, available=lambda: None)
    with pytest.raises(h4_coins.H4CoinsError, match="screen should give"):
        _decide(tmp_path, {1: 1, 2: 2}, free=1000, four=["XRPUSDT", "BTCUSDT", "ETHUSDT", "BNBUSDT"])
    with pytest.raises(h4_coins.H4CoinsError, match="operator"):
        _decide(tmp_path, {1: 1, 2: 2}, free=1000, operator=" ")
    assert not (tmp_path / h4_coins.DECISION_FILE).exists()


def test_a_damaged_or_missing_decision_blocks_the_run(tmp_path):
    with pytest.raises(h4_coins.H4CoinsError, match="not decided"):
        h4_coins.load(tmp_path)
    (tmp_path / h4_coins.DECISION_FILE).write_text('{"strategy": "H4", "coins": ["XRPUSDT"]}')
    with pytest.raises(h4_coins.H4CoinsError, match="damaged"):
        h4_coins.load(tmp_path)


def _write_history(tmp_path, symbols, days=80):
    start = RESEARCH_START_MS - 64 * DAY_MS
    for i, s in enumerate(symbols):
        history.write_csv(history._csv_path(tmp_path, s, "1h"), walk(start, days * 24, "100", 3 + i, step_ms=HOUR))
        history._rules_path(tmp_path, s).write_text(json.dumps(history.rules_to_info(rules([s])[s])))


def test_the_measurement_runs_h4_in_a_fresh_process_and_returns_only_memory(tmp_path):
    _write_history(tmp_path, ["BTCUSDT"])
    mb = h4_coins.measure_peak_mb(tmp_path, ["BTCUSDT"], AppConfig(), None)
    assert isinstance(mb, int) and 20 < mb < 2000
    with pytest.raises(h4_coins.H4CoinsError, match="ETHUSDT"):
        h4_coins.measure_peak_mb(tmp_path, ["ETHUSDT"], AppConfig(), None)      # no history: reported, not hidden


# ---------------------------------------------------------------- the CLI, and never the holdout
def test_the_cli_offers_h4_alone_and_never_on_the_holdout(tmp_path, capsys):
    assert isinstance(cli._make_strategy("H4", SYMS), TrendPullback)
    assert "H4" in cli.STRATEGIES and "H4" not in cli.HOLDOUT_STRATEGIES and "H4" not in cli.DEFAULT_RUN
    with pytest.raises(SystemExit):
        cli.main(["backtest", "holdout", "--strategy", "H4", "--operator", "Sultan", "--confirm", "x"])
    with pytest.raises(SystemExit):
        cli.main(["backtest", "run", "--interval", "1h"])                   # only fetch takes 1h
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h",
                              strategy=["H4", "H2b"], random_runs=1, reports_dir=str(tmp_path / "r"), screened=True)
    assert cli._backtest_run(args, out=io.StringIO()) == 2
    assert "on its own" in capsys.readouterr().err
    args.strategy = ["H4"]
    assert cli._backtest_run(args, out=io.StringIO()) == 2
    assert "not decided yet" in capsys.readouterr().err


def test_fetch_takes_the_1h_interval(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_backtest_fetch", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["backtest", "fetch", "--interval", "1h", "--screened"])
    assert seen["args"].interval == "1h" and seen["args"].screened


def test_the_run_uses_the_recorded_coins_and_1h_candles(tmp_path, monkeypatch, capsys):
    from bot.backtest import selection, universe
    _write_history(tmp_path, SYMS)
    rec, _ = _decide(tmp_path, {1: 100, 2: 150}, free=100)                # BTC and ETH only
    seen = {}

    def evaluate(strategy, candles, rules_, cfg, cost, random_runs, progress, corr_groups):
        seen.update(strategy=strategy, candles=candles, cfg=cfg, groups=corr_groups)
        raise RuntimeError("stop here")
    monkeypatch.setattr(selection, "evaluate", evaluate)
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 1000)
    args = argparse.Namespace(config="config/default.yaml", data_dir=str(tmp_path), interval="4h", strategy=["H4"],
                              random_runs=1, reports_dir=str(tmp_path / "r"), screened=True)
    with pytest.raises(RuntimeError, match="stop here"):
        cli._backtest_run(args, out=io.StringIO())
    assert isinstance(seen["strategy"], TrendPullback) and seen["strategy"].symbols == SYMS
    assert sorted(seen["candles"]) == SYMS and seen["cfg"].universe == SYMS and seen["groups"] is None
    c = seen["candles"]["BTCUSDT"]
    assert c[1].open_time - c[0].open_time == HOUR
    order = []
    real_load = history.load
    monkeypatch.setattr(history, "load", lambda *a: order.append("load") or real_load(*a))
    monkeypatch.setattr(universe, "available_memory_mb", lambda: order.append("memory") or 1000)
    with pytest.raises(RuntimeError, match="stop here"):
        cli._backtest_run(args, out=io.StringIO())
    assert order[0] == "memory" and "load" in order          # free memory is read before any candle is loaded
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 150 + 29)
    assert cli._backtest_run(args, out=io.StringIO()) == 2               # the measured peak plus the margin
    assert "150 MB" in capsys.readouterr().err
