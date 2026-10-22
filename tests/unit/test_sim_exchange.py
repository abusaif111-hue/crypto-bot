"""Module 3: the paper-trading exchange. Every entry goes through evaluate()."""
from dataclasses import replace
from decimal import Decimal as Dec

import pytest

import bot.paper.sim as sim_mod
from bot.core.config import AppConfig
from bot.core.money import is_multiple
from bot.exchange.interface import Candle, SpotReadVenue
from bot.paper import fills
from bot.paper.sim import STARTING_QUOTE, SimExchange
from bot.risk.models import Mode, RejectCode, TradeIntent
from tests.fakes import Clock, FakeMarket

INTENT = TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=Dec("80000"), stop_price=Dec("78000"),
                     take_profit_price=Dec("86000"), strategy="H1-trend", horizon_hours=96,
                     invalidation="daily close back below the breakout level")


def make(resume=True, **kw):
    clock, market = Clock(), FakeMarket()
    sim = SimExchange(AppConfig(), market, clock=clock, **kw)
    if resume:
        sim.resume("sultan", "paper trading session start")
    return sim, market, clock


def opened(**kw):
    sim, market, clock = make(**kw)
    res = sim.submit(INTENT)
    assert res.filled, res.reason
    return sim, market, clock, res


def test_starts_with_10000_usdt_and_matches_the_venue_interface():
    sim, _, _ = make(resume=False)
    assert STARTING_QUOTE == Dec("10000")
    assert sim.quote == Dec("10000") and sim.equity() == Dec("10000")
    assert [(b.asset, b.free, b.locked) for b in sim.balances()] == [("USDT", Dec("10000"), Dec("0"))]
    assert sim.taker_fee("BTCUSDT") == Dec("0.001")
    assert isinstance(sim, SpotReadVenue)


def test_entries_blocked_until_an_operator_resumes():
    sim, _, _ = make(resume=False)
    res = sim.submit(INTENT)
    assert not res.filled and res.decision.code is RejectCode.MODE
    assert sim.quote == Dec("10000") and not sim.fills


def test_every_submit_calls_the_risk_engine(monkeypatch):
    calls = []
    real = sim_mod.evaluate

    def spy(*a, **k):
        calls.append(a[0])
        return real(*a, **k)

    monkeypatch.setattr(sim_mod, "evaluate", spy)
    sim, market, _ = make()
    sim.submit(INTENT)
    sim.submit(replace(INTENT, symbol="ETHUSDT", entry_price=Dec("2500"), stop_price=Dec("2400"),
                       take_profit_price=Dec("2750")))
    sim.submit(INTENT)                                   # rejected: already holding BTC
    assert [i.symbol for i in calls] == ["BTCUSDT", "ETHUSDT", "BTCUSDT"]


def test_a_rejected_intent_changes_nothing():
    sim, _, _ = make()
    res = sim.submit(replace(INTENT, take_profit_price=Dec("80500")))   # reward-to-risk too low
    assert not res.filled and res.decision.code is RejectCode.REWARD_RISK
    assert sim.quote == Dec("10000") and not sim.positions and not sim.fills


def test_entry_fill_has_slippage_fee_and_engine_size():
    sim, _, _, res = opened()
    f = res.fill
    assert f.qty == res.decision.qty and f.qty > 0
    assert f.price == Dec("80040")                       # ask 80,000 + 0.05% slippage
    assert f.fee == f.qty * f.price * Dec("0.001")
    assert sim.quote == Dec("10000") - f.qty * f.price - f.fee
    pos = sim.positions["BTCUSDT"]
    assert (pos.stop_price, pos.take_profit_price) == (Dec("78000"), Dec("86000"))
    assert [(b.asset, b.locked) for b in sim.balances()][1] == ("BTC", f.qty)   # locked by the exit orders
    assert is_multiple(f.qty, Dec("0.00001"))


def test_no_fill_above_the_worst_entry_the_engine_sized_for():
    sim, market, _ = make()
    market.set_bid("BTCUSDT", "79995", spread="10")      # ask 80,005: + slippage is above 80,040
    res = sim.submit(INTENT)
    assert res.decision.accepted and not res.filled and "worst planned entry" in res.reason
    assert sim.quote == Dec("10000") and not sim.positions


def test_no_fill_when_the_ask_is_already_below_the_stop():
    sim, market, _ = make()
    market.set_bid("BTCUSDT", "77980", spread="10")
    res = sim.submit(INTENT)
    assert not res.filled and sim.quote == Dec("10000")


def test_stop_loss_exit_loses_no_more_than_planned():
    sim, market, clock, res = opened()
    clock.advance(60)
    market.set_bid("BTCUSDT", "78000")
    (t,) = sim.poll()
    assert t.exit_reason == fills.STOP_LOSS and not t.gapped
    assert t.exit_price == Dec("77610")                  # 78,000 - 0.5% BTC stop slippage
    assert t.exit_fee == t.qty * t.exit_price * Dec("0.001")
    assert t.pnl < 0 and -t.pnl <= t.planned_risk
    assert sim.quote == Dec("10000") + t.pnl and not sim.positions
    assert sim.equity() == sim.quote


def test_gap_through_the_stop_fills_lower_and_is_flagged():
    sim, market, clock, _ = opened()
    market.set_bid("BTCUSDT", "75000")
    (t,) = sim.poll()
    assert t.gapped and t.exit_price == Dec("75000") * Dec("0.995")
    assert -t.pnl > t.planned_risk                       # a real gap can lose more than planned


def test_eth_uses_its_wider_stop_slippage():
    sim, market, _ = make()
    res = sim.submit(replace(INTENT, symbol="ETHUSDT", entry_price=Dec("2500"), stop_price=Dec("2400"),
                             take_profit_price=Dec("2750")))
    assert res.filled, res.reason
    market.set_bid("ETHUSDT", "2400")
    (t,) = sim.poll()
    assert t.exit_price == Dec("2376")                   # 2,400 - 1.0%
    assert -t.pnl <= t.planned_risk


def test_take_profit_exit():
    sim, market, _, res = opened()
    market.set_bid("BTCUSDT", "86000")
    (t,) = sim.poll()
    assert t.exit_reason == fills.TAKE_PROFIT and t.exit_price == Dec("86000")
    assert t.pnl == t.qty * (Dec("86000") - Dec("80040")) - t.entry_fee - t.exit_fee
    assert t.pnl > 0
    ids = [f.client_order_id for f in sim.fills]
    assert len(set(ids)) == 2 and ids[0].startswith("sbe0") and ids[1].startswith("sbp1")


def test_time_stop_sells_at_market_when_the_horizon_ends():
    sim, market, clock, _ = opened()
    clock.advance(95 * 3600)
    assert sim.poll() == []
    clock.advance(3600)
    (t,) = sim.poll()
    assert t.exit_reason == fills.TIME_STOP and t.exit_price == Dec("79990") * Dec("0.995")


def test_candle_touching_both_exits_counts_as_a_stop():
    sim, _, clock, _ = opened()
    start_ms = int(clock() * 1000)
    c = Candle(open_time=start_ms, open=Dec("80000"), high=Dec("87000"), low=Dec("77000"), close=Dec("85000"),
               volume=Dec(1), close_time=start_ms + 3_599_999, quote_volume=Dec(1), closed=True)
    t = sim.on_candle("BTCUSDT", c)
    assert t.exit_reason == fills.STOP_LOSS and t.exit_price == Dec("77610") and not t.gapped


def test_candles_from_before_the_entry_and_unfinished_candles_are_ignored():
    sim, _, clock, _ = opened()
    start_ms = int(clock() * 1000)
    early = Candle(open_time=start_ms - 1000, open=Dec("80000"), high=Dec("90000"), low=Dec("70000"),
                   close=Dec("80000"), volume=Dec(1), close_time=start_ms + 1000, quote_volume=Dec(1), closed=True)
    forming = replace(early, open_time=start_ms, closed=False)
    assert sim.on_candle("BTCUSDT", early) is None and sim.on_candle("BTCUSDT", forming) is None
    assert "BTCUSDT" in sim.positions


def test_market_data_failure_blocks_entries():
    sim, market, _ = make()
    market.fail.add("books")
    res = sim.submit(INTENT)
    assert not res.filled and res.decision.code is RejectCode.STALE_DATA
    assert sim.quote == Dec("10000")


def test_correlation_group_blocks_a_third_major(monkeypatch):
    cfg = AppConfig(universe=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    clock, market = Clock(), FakeMarket(("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    market.set_bid("SOLUSDT", "199.99", spread="0.01")
    market.avg["SOLUSDT"] = Dec("200")
    sim = SimExchange(cfg, market, clock=clock)
    sim.resume("sultan", "paper trading session start")
    assert sim.submit(INTENT).filled
    assert sim.submit(replace(INTENT, symbol="ETHUSDT", entry_price=Dec("2500"), stop_price=Dec("2400"),
                              take_profit_price=Dec("2750"))).filled
    res = sim.submit(replace(INTENT, symbol="SOLUSDT", entry_price=Dec("200"), stop_price=Dec("190"),
                             take_profit_price=Dec("220")))
    assert res.decision.code is RejectCode.CORRELATION   # unknown coins count as majors


def test_daily_loss_budget_stops_new_entries():
    sim, market, clock = make()
    # Stopped-out trades of up to 0.25% each, until the 1.5% daily budget has no room for another.
    for _ in range(10):
        market.set_bid("BTCUSDT", "79990")
        res = sim.submit(INTENT)
        if not res.filled:
            break
        market.set_bid("BTCUSDT", "78000")
        sim.poll()
    assert not res.filled and len(sim.trades) >= 5
    assert res.decision.code in (RejectCode.RISK_BUDGET, RejectCode.EXCHANGE_MIN, RejectCode.DAILY_LOSS)
    losses = -sum(t.pnl for t in sim.trades)
    assert losses <= Dec("150")                          # never more than 1.5% of 10,000 lost in a day
    assert sim.account_state().daily_pnl == -losses


def test_drawdown_breaker_moves_the_mode_out_of_running():
    sim, market, clock = make()
    sim.quote -= Dec("700")                              # simulate a 7% loss directly
    state = sim.account_state()
    assert state.mode is Mode.ENTRIES_BLOCKED and RejectCode.DRAWDOWN.value in sim.modes.active_breaches
    res = sim.submit(INTENT)
    assert not res.filled and res.decision.code is RejectCode.MODE


def test_new_day_resets_the_daily_pnl_baseline():
    sim, market, clock, _ = opened()
    market.set_bid("BTCUSDT", "78000")
    (t,) = sim.poll()
    clock.advance(86400)
    state = sim.account_state()
    assert state.daily_pnl == 0 and state.day_start_equity == Dec("10000") + t.pnl
    assert state.weekly_pnl == t.pnl


def test_the_simulator_never_reads_real_balances_or_fees():
    sim, market, clock, _ = opened()          # FakeMarket.balances / taker_fee raise if called
    market.set_bid("BTCUSDT", "86000")
    sim.poll()
    assert sim.equity() == sim.quote


def test_bad_settings_are_refused():
    for kw in ({"starting_quote": Dec("0")}, {"fee_rate": Dec("-0.001")}, {"entry_slippage_pct": Dec("-1")}):
        with pytest.raises(ValueError):
            SimExchange(AppConfig(), FakeMarket(), clock=Clock(), **kw)
