"""The H1-DailyTrader study (`bot.backtest.daily_trader_study`, `bot backtest h1dt`): fixed windows ending
2024-10-01, nothing from the reserved final window or the holdout, the selection rule unchanged. Made-up
candles; nothing here reads Binance."""
import argparse
import io
import random
from collections import Counter
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.backtest import daily_trader_study as study
from bot.backtest import selection
from bot.backtest.periods import HOLDOUT_START_MS
from bot.backtest.walkforward import Window, _ms
from bot.core.config import AppConfig
from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Candle
from tests.conftest import exchange_info_symbol

H = 3_600_000


def walk(seed, p0, n, t0, step_ms=H):
    r, out, p = random.Random(seed), [], p0
    for i in range(n):
        o = p
        p = p * (1 + r.gauss(0.00005, 0.006))
        hi, lo = max(o, p) * (1 + abs(r.gauss(0, 0.002))), min(o, p) * (1 - abs(r.gauss(0, 0.002)))
        t = t0 + i * step_ms
        q = lambda x: Dec(str(round(x, 2)))                                  # noqa: E731
        out.append(Candle(t, q(o), q(hi), q(lo), q(p), Dec(10), t + step_ms - 1, Dec("6e7"), True))
    return out


RULES = {s: parse_symbol(exchange_info_symbol(s)) for s in study.SYMBOLS}


def test_windows_are_the_walk_forward_rule_ending_before_the_reserved_window():
    ws = study.windows()
    assert [w.label for w in ws] == ["2022-01 to 2022-06", "2022-07 to 2022-12", "2023-01 to 2023-06",
                                     "2023-07 to 2023-12", "2024-01 to 2024-06", "2024-07 to 2024-09"]
    assert ws[-1].test_end == study.STUDY_END_MS == _ms(2024, 10) < HOLDOUT_START_MS
    assert all(w.train_end - w.train_start > 700 * 86_400_000 for w in ws)          # 24 months of training


def test_study_only_drops_every_candle_from_the_reserved_window_on():
    cs = walk(1, 100, 10, study.STUDY_END_MS - 5 * H)
    kept = study.study_only(cs)
    assert len(kept) == 5 and all(c.close_time < study.STUDY_END_MS for c in kept)


def test_four_strategies_on_their_own_candles():
    names = [(s.name, iv) for s, iv in study.strategies()]
    assert names == [("H1DT-BRK", "1h"), ("H1DT-CONT", "1h"), ("H1DT-RNG", "1h"), ("D1", "4h")]


def test_run_study_cuts_the_candles_and_uses_the_fixed_windows_and_groups(monkeypatch):
    seen = []

    def fake(strategy, candles, rules, cfg, cost, random_runs, progress, ws, corr_groups):
        seen.append((strategy.name, {c.close_time < study.STUDY_END_MS for cs in candles.values() for c in cs},
                     ws, corr_groups, strategy.interval))
        return selection.StrategyEvaluation(strategy.name, strategy.params(), [], 0, Dec(0), None, 0, None,
                                            None, None, None, 0, [], {}, {}, [], {}, {})
    monkeypatch.setattr(selection, "evaluate", fake)
    late = study.STUDY_END_MS + 10 * H
    candles = {"1h": {s: walk(1, 100, 30, late - 20 * H) for s in study.SYMBOLS},
               "4h": {s: walk(2, 100, 30, late - 80 * H, 4 * H) for s in study.SYMBOLS}}
    rows = study.run_study(candles, RULES, AppConfig())
    assert [r.name for r in rows] == ["H1DT-BRK", "H1DT-CONT", "H1DT-RNG", "D1"]
    for name, flags, ws, groups, interval in seen:
        assert flags == {True} and ws == study.windows() and groups == study.CORR_GROUPS
    assert [x[4] for x in seen] == ["1h", "1h", "1h", "4h"]
    assert [r.name for r in study.run_study(candles, RULES, AppConfig(), only={"D1"})] == ["D1"]


def test_a_real_short_run_through_the_selection_rule_renders_every_figure(monkeypatch):
    # one small window in 2020 so the test stays fast; the real study uses study.windows()
    monkeypatch.setattr(study, "windows", lambda: [Window(_ms(2020, 2), _ms(2020, 3), _ms(2020, 3), _ms(2020, 4))])
    t0 = _ms(2020, 1)
    candles = {"1h": {"BTCUSDT": walk(1, 30000, 24 * 92, t0), "ETHUSDT": walk(2, 2000, 24 * 92, t0)}}
    rows = study.run_study(candles, RULES, AppConfig(), random_runs=1, only={"H1DT-BRK"})
    [r] = rows
    assert r.ev.windows and isinstance(r.outcomes, Counter) and len(r.ev.checks) == 7
    text = study.render(rows)
    for part in ("H1DT-BRK", "Trades", "Exp/trade", "Doubled", "PF", "Worst DD", "Costs", "Checks",
                 "Ideas and what the risk engine did", "the holdout and the intraday plan's final window"):
        assert part in text, part
    s = study.summary(rows)["H1DT-BRK"]
    assert {"oos_trades", "oos_expectancy", "profit_factor", "worst_window_drawdown", "costs", "checks"} <= set(s)


def test_cli_refuses_without_enough_memory(monkeypatch, tmp_path, capsys):
    from bot.backtest import universe
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 50)
    a = argparse.Namespace(data_dir=str(tmp_path), reports_dir=str(tmp_path), random_runs=1, only=None,
                           config="config/default.yaml")
    assert cli._backtest_h1dt(a, out=io.StringIO()) == 2
    assert "memory" in capsys.readouterr().err


def test_cli_reports_missing_history(monkeypatch, tmp_path, capsys):
    from bot.backtest import universe
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 5000)
    a = argparse.Namespace(data_dir=str(tmp_path), reports_dir=str(tmp_path), random_runs=1, only=None,
                           config="config/default.yaml")
    assert cli._backtest_h1dt(a, out=io.StringIO()) == 2
    assert "no history" in capsys.readouterr().err


def test_cli_parser_has_the_command():
    with pytest.raises(SystemExit):
        cli.main(["backtest", "h1dt", "--help"])
