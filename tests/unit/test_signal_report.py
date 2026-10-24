"""`bot signals`: how close H2b is to buying, coin by coin (read-only)."""
import argparse
import ast
from decimal import Decimal as Dec
from pathlib import Path

from bot import cli, forward, signal_report
from bot.analysis.indicators import percentile
from bot.backtest.periods import RESEARCH_START_MS
from bot.core.config import AppConfig
from bot.strategies.h2b_blend import BlendedMomentum
from tests.unit.test_h2b import CALM_RISE, path
from tests.unit.test_strategies import Source, series

ROOT = Path(__file__).resolve().parents[2]
H2B = BlendedMomentum(["BTCUSDT"]).with_params(threshold=Dec("0.60"), atr_mult=Dec("3"))


def four_hourly(daily_closes):
    """Six 4h candles a day, the day's close on the last one (days start at 00:00 UTC)."""
    return series([c for c in daily_closes for _ in range(6)], start=RESEARCH_START_MS)


def one(closes, strategy=H2B):
    return signal_report.coin_signal(strategy, Source({"BTCUSDT": four_hourly(closes)}), "BTCUSDT")


def test_the_verdict_is_h2bs_own():
    """The report never decides anything itself: enter/why come from BlendedMomentum.signal()."""
    for closes in (path(CALM_RISE), path([(380, 0.0, 0.02), (60, -0.004, 0.01)]), path(CALM_RISE)[:200]):
        cs = four_hourly(closes)[-H2B.history_needed():]
        s = one(closes)
        assert (s.enter, s.why) == H2B.signal(H2B._daily(cs))


def test_a_calm_rise_shows_buy_conditions_and_the_stop():
    s = one(path(CALM_RISE))
    assert s.enter and s.stop_pct is not None and s.stop_pct > 0
    assert sum(lb.strong for lb in s.lookbacks) >= 2 and s.paused is False
    text = signal_report.render([s], H2B)
    assert "BUY CONDITIONS MET" in text and "below the entry" in text and "risk engine still decides" in text


def test_a_falling_coin_shows_how_far_it_is_from_the_bar():
    s = one(path([(380, 0.0, 0.02), (60, -0.004, 0.01)]))
    assert not s.enter and "lookbacks strong" in s.why
    assert s.below_zero >= 2
    for lb in s.lookbacks:
        assert lb.needed == H2B.threshold * s.vol * Dec(lb.days).sqrt()
        assert lb.strong == (lb.ret > lb.needed)
    text = signal_report.render([s], H2B)
    assert "no entry" in text and "not yet" in text and "needs above" in text
    assert "sells at 2" in text and "BUY" not in text


def test_the_volatility_pause_is_shown():
    """A calm year, then a rise a little wilder than the year's top 20%: returns strong, entries paused."""
    s = one(path([(380, 0.0, 0.01), (60, 0.012, 0.014)]))
    assert not s.enter and s.why == "volatility pause" and s.paused is True
    assert s.pause_level < s.vol < s.pause_level * Dec("1.2")          # close to the level, just over it
    assert "ON: no new entries" in signal_report.render([s], H2B)


def test_the_numbers_are_worked_out_from_the_finished_days():
    """Returns, the exit vote and the pause level, recomputed here from the daily closes."""
    for seg in (CALM_RISE, [(380, 0.0, 0.01), (60, 0.012, 0.014)], [(380, 0.0, 0.02), (60, -0.004, 0.01)]):
        cs = four_hourly(path(seg))[-H2B.history_needed():]
        days = H2B._daily(cs)
        s = one(path(seg))
        rets = [days[-1].close / days[-1 - lb.days].close - 1 for lb in s.lookbacks]
        assert [lb.ret for lb in s.lookbacks] == rets and [lb.days for lb in s.lookbacks] == [14, 28, 56]
        assert s.below_zero == sum(1 for r in rets if r < 0)
        today = len(days) - 1
        assert s.vol == H2B._vol(days, today)
        assert s.pause_level == percentile([H2B._vol(days, i) for i in range(today - 365, today)], Dec("0.8"))
        assert s.paused == (s.vol >= s.pause_level)
        assert s.last_day_ms == days[-1].open_time
        assert (s.stop_pct is not None) == s.enter


def test_short_history_says_so_without_a_pause_reading():
    s = one(path(CALM_RISE)[:200])
    assert not s.enter and s.why == "not enough history" and s.paused is None
    assert "needs a year of history" in signal_report.render([s], H2B)


def test_a_market_data_error_shows_no_numbers():
    class Down:
        def closed_candles(self, *a, **k):
            raise ConnectionError("timeout")
    s = signal_report.coin_signal(H2B, Down(), "BTCUSDT")
    assert not s.enter and s.error == "ConnectionError: timeout" and not s.lookbacks
    text = signal_report.render([s], H2B)
    assert "could not read prices" in text and "BUY" not in text and "no entry" not in text


def test_report_covers_every_coin_and_shows_a_warning():
    s = BlendedMomentum(["BTCUSDT", "ETHUSDT"]).with_params(threshold=Dec("0.60"))
    closes = four_hourly(path(CALM_RISE))
    out = signal_report.report(s, Source({"BTCUSDT": closes, "ETHUSDT": closes}))
    assert [c.symbol for c in out] == ["BTCUSDT", "ETHUSDT"]
    assert "WARNING: changed" in signal_report.render(out, s, "changed")


def test_the_command_uses_the_forward_tests_settings_and_live_prices(monkeypatch, capsys):
    from bot.market import data as market_data
    seen = {}
    ft = argparse.Namespace(symbols=["BTCUSDT"])
    monkeypatch.setattr(cli, "load_config", lambda p: AppConfig())
    monkeypatch.setattr(forward, "load", lambda d: ft)
    monkeypatch.setattr(forward, "make_strategy", lambda f: H2B)

    def check_unchanged(f, cfg):
        seen["cfg"] = cfg
        raise forward.ForwardError("H2b's code changed")
    monkeypatch.setattr(forward, "check_unchanged", check_unchanged)

    def mds(exchange, cfg):
        seen["env"], seen["universe"] = exchange.environment, cfg.universe
        return Source({"BTCUSDT": four_hourly(path(CALM_RISE))})
    monkeypatch.setattr(market_data, "MarketDataService", mds)
    args = argparse.Namespace(config="x", forward_dir="~/f")
    assert cli._signals(args, make_exchange=lambda environment: argparse.Namespace(environment=environment)) == 0
    out = capsys.readouterr().out
    assert seen["env"] == "live" and seen["universe"] == ["BTCUSDT"]
    assert forward.config_sha256(seen["cfg"]) == forward.config_sha256(AppConfig())
    assert "WARNING: H2b's code changed" in out and "BUY CONDITIONS MET" in out and "threshold 0.60" in out


def test_the_command_stops_without_a_forward_test(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda p: AppConfig())

    def missing(d):
        raise forward.ForwardError("no forward test")
    monkeypatch.setattr(forward, "load", missing)
    assert cli._signals(argparse.Namespace(config="x", forward_dir="~/f")) == 2
    assert "no forward test" in capsys.readouterr().err


def test_the_command_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_signals", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["signals"])
    assert seen["args"].forward_dir == "~/forward-h2b"


def test_the_report_is_read_only():
    """It imports no trading, order or account code and writes nothing; H2b's code is untouched."""
    tree = ast.parse((ROOT / "src" / "bot" / "signal_report.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("bot.execution", "bot.exchange", "bot.risk", "bot.paper")) for m in mods)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"write_text", "write_bytes", "open", "mkdir", "unlink", "submit", "evaluate"}
    assert "open(" not in (ROOT / "src" / "bot" / "signal_report.py").read_text()
