"""Module 4: `bot backtest fetch` and `bot backtest baselines`. No network."""
import argparse
import io

from bot import cli
from bot.backtest.periods import RESEARCH_START_MS, WARMUP_START_MS
from tests.backtest_data import H4, walk
from tests.unit.test_backtest_data import HistoryVenue


class TwoCoinVenue(HistoryVenue):
    def __init__(self, n):
        self.data = {"BTCUSDT": walk(WARMUP_START_MS, n, "4000", 1), "ETHUSDT": walk(WARMUP_START_MS, n, "150", 2)}
        super().__init__([])

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        self.candles = self.data[symbol]
        return super().klines(symbol, interval, start_ms, end_ms, limit)


def args(**kw):
    base = dict(config="config/default.yaml", data_dir="", interval="4h", symbols=None, seeds=2, reports_dir="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_fetch_then_baselines(tmp_path, capsys):
    n = (RESEARCH_START_MS - WARMUP_START_MS) // H4 + 600
    venue = TwoCoinVenue(n)
    assert cli._backtest_fetch(args(data_dir=str(tmp_path / "data")), make_exchange=lambda **k: venue) == 0
    out = capsys.readouterr().out
    assert f"BTCUSDT 4h: {n} candles, 0 gaps" in out and "stays locked" in out
    buf = io.StringIO()
    assert cli._backtest_baselines(args(data_dir=str(tmp_path / "data"), reports_dir=str(tmp_path / "rep")),
                                   out=buf) == 0
    text = buf.getvalue()
    assert "Buy and hold BTC" in text and "random entries: 2 of 2 runs" in text and "Report saved in" in text
    assert list((tmp_path / "rep").glob("*-baselines/report.md"))


def test_baselines_without_data_explains_what_to_do(tmp_path, capsys):
    assert cli._backtest_baselines(args(data_dir=str(tmp_path)), out=io.StringIO()) == 2
    assert "bot backtest fetch" in capsys.readouterr().err


def test_fetch_failure_is_reported(tmp_path, capsys):
    class Broken(TwoCoinVenue):
        def klines(self, *a, **k):
            raise RuntimeError("Binance down")
    assert cli._backtest_fetch(args(data_dir=str(tmp_path)), make_exchange=lambda **k: Broken(1)) == 4
    assert "FAILED" in capsys.readouterr().err
