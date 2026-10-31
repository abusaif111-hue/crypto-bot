"""H5's orders the bot holds itself (`bot.execution.held`, runner step 5): kept, not sent, until the best ask
trades above the trigger within the limit; then entered the usual way (PENDING, `entry.submit()`, a fresh
risk check, protection attached). One attempt; expiry; dropped when entries are blocked; never for a coin
already held; kept across a restart; sized on at most H5's own capital."""
import json
from decimal import Decimal as Dec

import pytest

from bot.execution import held as held_orders
from bot.execution.ledger import Ledger, OPEN
from bot.execution.account import TradingAccount
from bot.execution.runner import RunnerError, TestnetRunner
from bot.risk.models import Mode, TradeIntent
from tests.unit.test_testnet_runner import T0, events, journal, next_candle, request, resume, world  # noqa: F401

DAY = 86_400


class H5Idea:
    name, interval = "H5", "4h"

    def __init__(self):
        self.ideas = []

    def propose(self, data, now):
        out, self.ideas = self.ideas, []
        return out

    def manage(self, data, now, open_trades):
        return []


def advance(now, trigger="81000", limit="81405", stop="78000"):
    t, s = Dec(trigger), Dec(stop)
    return TradeIntent("BTCUSDT", "BUY", Dec(limit), s, t + 3 * (t - s), "H5", 720,
                       "a daily close below the 10-day low", trigger_price=t, valid_until=now + DAY)


def price(w, ask):
    a = Dec(ask)
    w.market.set_bid("BTCUSDT", a - Dec("10"))
    w.ex.ask, w.ex.bid = a, a - Dec("10")


@pytest.fixture
def h5(world):  # noqa: F811
    s = world.runner.strategies[0]
    idea = H5Idea()
    world.runner.strategies = [s, idea]
    world.h5 = idea
    resume(world)
    return world


def place(w):
    next_candle(w)
    w.h5.ideas = [advance(w.clock())]
    w.runner.tick()


def test_an_advance_idea_is_held_not_sent_and_kept_on_disk(h5):
    place(h5)
    assert "BTCUSDT" in h5.runner.held and not [x for x in h5.ex.sent if x[0] == "opoco"]
    rows = json.loads((h5.dir / held_orders.HELD_FILE).read_text())
    assert rows[0]["symbol"] == "BTCUSDT" and rows[0]["trigger"] == "81000"
    assert events(h5.dir, "held_order_placed")


def test_sent_only_once_the_ask_trades_above_the_trigger(h5):
    place(h5)
    price(h5, "81000")                                          # at the trigger: not above it
    h5.runner.tick()
    assert "BTCUSDT" in h5.runner.held and not [x for x in h5.ex.sent if x[0] == "opoco"]
    price(h5, "81100")
    h5.clock.advance(60)
    h5.runner.tick()
    assert [x for x in h5.ex.sent if x[0] == "opoco"] == [("opoco", "BTCUSDT")]
    (t,) = [t for t in h5.runner.ledger.trades.values() if t.strategy == "H5"]
    assert t.status == OPEN and "BTCUSDT" not in h5.runner.held
    assert events(h5.dir, "held_order_triggered") and events(h5.dir, "entry_protected")


def test_never_chases_past_the_limit_but_buys_if_the_price_comes_back(h5):
    place(h5)
    price(h5, "82000")                                          # above the 81,405 limit
    h5.runner.tick()
    assert "BTCUSDT" in h5.runner.held and not [x for x in h5.ex.sent if x[0] == "opoco"]
    price(h5, "81300")
    h5.clock.advance(60)
    h5.runner.tick()
    assert [x for x in h5.ex.sent if x[0] == "opoco"] == [("opoco", "BTCUSDT")]


def test_one_attempt_even_if_the_fill_or_kill_does_not_fill(h5):
    place(h5)
    price(h5, "81100")
    h5.ex.ask = Dec("81500")                                    # the exchange moved past the limit: FOK expires
    h5.runner.tick()
    assert "BTCUSDT" not in h5.runner.held and events(h5.dir, "entry_not_filled")
    h5.clock.advance(60)
    h5.runner.tick()
    assert len([x for x in h5.ex.sent if x[0] == "opoco"]) == 1


def test_expires_at_its_time(h5):
    place(h5)
    h5.clock.advance(DAY)
    h5.runner.tick()
    assert not h5.runner.held and events(h5.dir, "held_order_expired")


def test_all_held_orders_are_dropped_when_entries_are_blocked(h5):
    place(h5)
    request(h5.dir, "block", "operator stops new entries")
    h5.runner.tick()
    assert h5.runner.account.modes.mode is not Mode.RUNNING
    assert not h5.runner.held and events(h5.dir, "held_order_cancelled")
    price(h5, "81100")
    h5.runner.tick()
    assert not [x for x in h5.ex.sent if x[0] == "opoco"]


def test_never_held_for_a_coin_already_held(h5):
    next_candle(h5)
    h5.runner.strategies[0].ideas = [TradeIntent("BTCUSDT", "BUY", Dec("80080"), Dec("78000"), Dec("86320"),
                                                 "H2b", 720, "x")]
    h5.h5.ideas = [advance(h5.clock())]
    h5.runner.tick()
    assert "BTCUSDT" not in h5.runner.held and events(h5.dir, "held_order_skipped")


def test_held_orders_survive_a_restart_and_an_unreadable_file_stops_the_runner(h5):
    place(h5)
    w = h5
    w.runner.account.save()
    again = TestnetRunner(w.dir, w.cfg, w.ex, w.runner.exchange, w.runner.signals, w.runner.strategies,
                          {"BTCUSDT": "majors", "ETHUSDT": "majors"}, Ledger.load(w.dir, "testnet"),
                          TradingAccount.load(w.dir, w.cfg, w.clock()), clock=w.clock)
    assert again.held["BTCUSDT"].trigger == Dec("81000")
    (w.dir / held_orders.HELD_FILE).write_text("{oops")
    with pytest.raises(RunnerError, match="never reset"):
        TestnetRunner(w.dir, w.cfg, w.ex, w.runner.exchange, w.runner.signals, w.runner.strategies,
                      {"BTCUSDT": "majors"}, Ledger.load(w.dir, "testnet"),
                      TradingAccount.load(w.dir, w.cfg, w.clock()), clock=w.clock)
    assert (w.dir / held_orders.HELD_FILE).read_text() == "{oops"


def test_a_strategy_with_a_cap_is_sized_on_at_most_its_own_capital(h5):
    h5.runner.strategy_caps = {"H5": Dec("100")}
    next_candle(h5)
    h5.h5.ideas = [advance(h5.clock(), stop="79500")]           # a stop close enough for 100 USDT
    h5.runner.tick()
    price(h5, "81100")
    h5.runner.tick()
    e = events(h5.dir, "entry_protected")[-1]
    assert Dec(e["planned_risk"]) <= Dec("0.25")                # 0.25% of 100 USDT, not of the 1,000 capital


def test_at_100_usdt_a_wider_stop_is_refused_below_binances_minimum(h5):
    # Sultan's 100 USDT trial: with a stop about 3.7% away the order is under 5 USDT, so the engine refuses it
    h5.runner.strategy_caps = {"H5": Dec("100")}
    place(h5)
    price(h5, "81100")
    h5.runner.tick()
    (e,) = events(h5.dir, "entry_rejected")
    assert e["code"] == "REJ_BELOW_EXCHANGE_MIN" and not [x for x in h5.ex.sent if x[0] == "opoco"]


def test_the_held_order_is_an_ordinary_limit_intent_once_reached():
    o = held_orders.HeldOrder.from_intent(advance(T0), T0)
    assert o.reached(Dec("81000.01")) and not o.reached(Dec("81000")) and not o.reached(Dec("81405.01"))
    i = o.intent()
    assert i.trigger_price is None and i.valid_until is None and i.entry_price == Dec("81405")
    with pytest.raises(held_orders.HeldError):
        held_orders.HeldOrder.from_intent(TradeIntent("BTCUSDT", "BUY", Dec(1), Dec(0.5), Dec(3), "H5", 1, "x"), T0)


# ---------------------------------------------------------------- the H5 switch (Sultan's step)
def test_h5_is_switched_on_once_within_the_accounts_capital(tmp_path):
    rec = held_orders.enable_h5(tmp_path, "Sultan", Dec("100"), Dec("350"), T0)
    assert rec["capital"] == "100" and held_orders.load_h5(tmp_path) == Dec("100")
    with pytest.raises(held_orders.HeldError, match="already exists"):
        held_orders.enable_h5(tmp_path, "Sultan", Dec("100"), Dec("350"), T0)
    other = tmp_path / "x"
    other.mkdir()
    for cap, acct, op in ((Dec("400"), Dec("350"), "Sultan"), (Dec("0"), Dec("350"), "Sultan"),
                          (Dec("100"), Dec("350"), " ")):
        with pytest.raises(held_orders.HeldError):
            held_orders.enable_h5(other, op, cap, acct, T0)
    assert held_orders.load_h5(other) is None
    (other / held_orders.H5_FILE).write_text("{bad")
    with pytest.raises(held_orders.HeldError):
        held_orders.load_h5(other)


def test_the_runner_trades_h5_only_after_h5_enable(tmp_path):
    from bot import cli
    from bot.strategies.h5_breakout import DailyBreakout
    h2b = object()
    strategies, caps = cli._runner_strategies(tmp_path, h2b, ["BTCUSDT", "ETHUSDT"])
    assert strategies == [h2b] and caps == {}
    held_orders.enable_h5(tmp_path, "Sultan", Dec("100"), Dec("350"), T0)
    strategies, caps = cli._runner_strategies(tmp_path, h2b, ["BTCUSDT", "ETHUSDT"])
    assert strategies[0] is h2b and isinstance(strategies[1], DailyBreakout)
    assert strategies[1].symbols == ["BTCUSDT", "ETHUSDT"] and caps == {"H5": Dec("100")}


def test_h5_enable_command_and_status(tmp_path, capsys):
    import argparse
    import io
    from bot import cli
    (tmp_path / "account.json").write_text(json.dumps({"capital": "350"}))
    a = argparse.Namespace(state_dir=str(tmp_path), operator="Sultan", capital="100", env="live")
    out = io.StringIO()
    assert cli._h5_enable(a, out=out) == 0 and "at most 100" in out.getvalue() and "restart" in out.getvalue()
    assert cli._h5_enable(a, out=io.StringIO()) == 2 and "already exists" in capsys.readouterr().err
    big = argparse.Namespace(state_dir=str(tmp_path / "y"), operator="Sultan", capital="100", env="live")
    assert cli._h5_enable(big, out=io.StringIO()) == 2                       # no account there yet
