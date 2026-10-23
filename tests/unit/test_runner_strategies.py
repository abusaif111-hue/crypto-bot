"""Strategies in the paper runner: asked once per finished candle, their own exits, restarts and outages."""
import json
from decimal import Decimal as Dec

import pytest

from bot.paper.runner import CLOCK_FILE, RETRY_S, PaperRunner, write_request
from bot.paper.store import StateError
from bot.risk.models import TradeIntent
from bot.strategies.base import Exit, RaiseStop
from tests.unit.test_paper_runner import events, journal, opened, trade_request

H4 = 4 * 3600


def btc_idea(strategy="S"):
    return TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=Dec("80100"), stop_price=Dec("78000"),
                       take_profit_price=Dec("86000"), strategy=strategy, horizon_hours=720, invalidation="x")


class Recorder:
    """A 4h strategy that records every call; proposes `ideas` once, then manages with `actions`."""
    name, interval = "S", "4h"

    def __init__(self, ideas=(), actions=(), fail_times=0):
        self.ideas, self.actions, self.fail_times = list(ideas), list(actions), fail_times
        self.proposed, self.managed, self.attempts = [], [], 0

    def propose(self, data, now):
        self.attempts += 1
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("klines: timeout")
        self.proposed.append(now)
        out, self.ideas = self.ideas, []
        return out

    def manage(self, data, now, open_trades):
        self.managed.append((now, [t.symbol for t in open_trades]))
        return self.actions


def resumed(tmp_path, **kw):
    runner, market, clock = opened(tmp_path, **kw)
    write_request(tmp_path, "resume", operator="sultan", reason="start paper trading")
    return runner, market, clock


def ticks(runner, clock, seconds, every=5):
    for _ in range(int(seconds // every)):
        runner.tick()
        clock.advance(every)


def test_asked_once_per_finished_candle_not_every_tick(tmp_path):
    s = Recorder()
    runner, _, clock = resumed(tmp_path, strategies=[s])
    ticks(runner, clock, 3600)                                  # an hour of 5-second ticks: one decision
    assert len(s.proposed) == 1
    clock.advance(H4)                                           # the next 4h candle has finished
    ticks(runner, clock, 60)
    assert len(s.proposed) == 2


def test_a_restart_neither_repeats_nor_skips_a_decision(tmp_path):
    s = Recorder()
    runner, market, clock = resumed(tmp_path, strategies=[s])
    runner.tick()
    assert json.loads((tmp_path / CLOCK_FILE).read_text()) == {"S": int(clock() * 1000) // (H4 * 1000) * H4 * 1000}
    clock.advance(600)
    again = Recorder()
    PaperRunner.open(tmp_path, runner.sim.cfg, market, clock, strategies=[again]).tick()
    assert again.proposed == []                                 # same candle: already decided before the restart
    clock.advance(H4)
    after = Recorder()
    PaperRunner.open(tmp_path, runner.sim.cfg, market, clock, strategies=[after]).tick()
    assert len(after.proposed) == 1 and "decisions_missed" not in events(tmp_path)


def test_candles_missed_while_down_are_logged(tmp_path):
    s = Recorder()
    runner, market, clock = resumed(tmp_path, strategies=[s])
    runner.tick()
    clock.advance(3 * H4)                                       # down for three candle closes
    PaperRunner.open(tmp_path, runner.sim.cfg, market, clock, strategies=[Recorder()]).tick()
    (missed,) = [e for e in journal(tmp_path) if e["event"] == "decisions_missed"]
    assert missed["strategy"] == "S" and missed["candles"] == 2


def test_a_failing_strategy_is_logged_once_and_retried(tmp_path):
    s = Recorder(fail_times=2)
    runner, _, clock = resumed(tmp_path, strategies=[s])
    ticks(runner, clock, 3 * RETRY_S + 30)
    assert events(tmp_path).count("strategy_error") == 1       # not every 5 seconds
    assert len(s.proposed) == 1                                 # the third try succeeded, then no more this candle
    down = Recorder(fail_times=10**6)
    runner2, _, clock2 = resumed(tmp_path / "down", strategies=[down])
    ticks(runner2, clock2, 3 * RETRY_S + 30)
    assert down.attempts == 4                                   # at 0, 60, 120 and 180 s: not every tick


def test_the_strategy_exits_only_its_own_positions(tmp_path):
    manual_only = Recorder(actions=[Exit("BTCUSDT", "turned")])
    runner, _, clock = resumed(tmp_path, strategies=[manual_only])
    trade_request(tmp_path)                                     # a manual trade: not the strategy's
    runner.tick()
    clock.advance(H4)
    runner.tick()
    assert "BTCUSDT" in runner.sim.positions and manual_only.managed == []


def test_strategy_exit_sells_at_market_and_is_journalled(tmp_path):
    s = Recorder(ideas=[btc_idea()])
    runner, _, clock = resumed(tmp_path, strategies=[s])
    runner.tick()
    assert runner.sim.positions["BTCUSDT"].strategy == "S"
    s.actions = [RaiseStop("BTCUSDT", Dec("79000"))]
    clock.advance(H4)
    runner.tick()
    assert runner.sim.positions["BTCUSDT"].stop_price == Dec("79000") and "stop_raised" in events(tmp_path)
    s.actions = [Exit("BTCUSDT", "2 of the 14/28/56-day returns turned negative")]
    clock.advance(H4)
    runner.tick()
    assert "BTCUSDT" not in runner.sim.positions
    ev = events(tmp_path)
    assert ev.index("strategy_exit") < ev.index("trade_closed")
    closed = [e for e in journal(tmp_path) if e["event"] == "trade_closed"][0]
    assert closed["exit_reason"] == "STRATEGY_EXIT" and s.managed[-1][1] == ["BTCUSDT"]


def test_two_strategies_with_one_name_are_refused(tmp_path):
    with pytest.raises(ValueError):
        opened(tmp_path, strategies=[Recorder(), Recorder()])


def test_an_unreadable_clock_file_stops_the_runner(tmp_path):
    opened(tmp_path)
    (tmp_path / CLOCK_FILE).write_text("{not json")
    with pytest.raises(StateError):
        opened(tmp_path)


def test_h2b_in_the_runner_fetches_history_once_per_candle(tmp_path):
    from bot.strategies.h2b_blend import BlendedMomentum
    runner, market, clock = resumed(tmp_path, strategies=[BlendedMomentum(["BTCUSDT", "ETHUSDT"])])
    ticks(runner, clock, 3600)
    assert market.calls.count("klines") == 2                    # one per coin in an hour, not 720 per coin
    assert "strategy_error" not in events(tmp_path)             # no history yet: simply no ideas
