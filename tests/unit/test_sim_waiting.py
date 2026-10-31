"""Orders placed in advance (H5, docs/h5-plan.md section 4): checked by the risk engine when placed, counted as
open while waiting, filled only once the price trades above the trigger and never above the limit, expired,
replaced, cancelled when entries are blocked. An intent without a trigger fills exactly as before."""
from dataclasses import replace
from decimal import Decimal as Dec

from bot.core.config import AppConfig
from bot.exchange.interface import Candle
from bot.paper import fills
from bot.paper.sim import SimExchange
from bot.risk.models import RejectCode, TradeIntent
from tests.fakes import Clock, FakeMarket

DAY = 86_400
H4 = 4 * 3600


def make():
    clock, market = Clock(), FakeMarket()
    sim = SimExchange(AppConfig(), market, clock=clock)
    sim.resume("sultan", "test session start")
    return sim, market, clock


def advance(clock, **kw):
    base = dict(symbol="BTCUSDT", side="BUY", entry_price=Dec("82410"), stop_price=Dec("79000"),
                take_profit_price=Dec("91000"), strategy="H5", horizon_hours=720,
                invalidation="a daily close below the 10-day low", trigger_price=Dec("82000"),
                valid_until=clock() + DAY)
    base.update(kw)
    return TradeIntent(**base)


def candle(t, o, h, lo, c, hours=4):
    ms = int(t * 1000)
    return Candle(ms, Dec(o), Dec(h), Dec(lo), Dec(c), Dec(1), ms + hours * 3_600_000 - 1, Dec(1), True)


def placed():
    sim, market, clock = make()
    res = sim.submit(advance(clock))
    assert res.waiting and not res.filled, res.reason
    return sim, market, clock, res


def test_an_advance_order_is_checked_then_waits_and_spends_nothing():
    sim, _, clock, res = placed()
    assert res.decision.accepted and "BTCUSDT" in sim.waiting and not sim.positions
    assert sim.quote == Dec("10000") and not sim.fills and sim.waiting_log["placed"] == 1


def test_a_waiting_order_counts_as_open_for_every_limit():
    sim, _, clock, _ = placed()
    now_intent = TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=Dec("80000"), stop_price=Dec("78000"),
                             take_profit_price=Dec("86000"), strategy="H5", horizon_hours=96, invalidation="x")
    r = sim.submit(now_intent)
    assert not r.filled and r.decision.code is RejectCode.EXISTING_POSITION         # R-14: as if already held
    state = sim.account_state()
    [p] = state.positions
    assert p.symbol == "BTCUSDT" and p.avg_entry == Dec("82410") and p.current_bid == Dec("82410")


def test_no_fill_until_the_price_trades_above_the_trigger():
    sim, _, clock, _ = placed()
    assert sim.on_candle("BTCUSDT", candle(clock(), "80000", "82000", "79500", "81000")) is None
    assert "BTCUSDT" in sim.waiting and not sim.positions                       # touching is not trading above


def test_fills_at_the_trigger_with_slippage_and_the_engines_size():
    sim, _, clock, res = placed()
    sim.on_candle("BTCUSDT", candle(clock(), "81000", "82500", "80800", "82300"))
    pos = sim.positions["BTCUSDT"]
    assert pos.entry_price == Dec("82000") * Dec("1.0005") and pos.qty == res.decision.qty
    assert pos.stop_price == Dec("79000") and pos.take_profit_price == Dec("91000")
    assert "BTCUSDT" not in sim.waiting and sim.waiting_log["filled"] == 1
    assert sim.quote < Dec("10000") and sim.fills[-1].kind == "ENTRY"


def test_a_gap_above_the_trigger_fills_at_the_open_if_within_the_limit():
    sim, _, clock, _ = placed()
    sim.on_candle("BTCUSDT", candle(clock(), "82200", "82900", "82100", "82800"))
    assert sim.positions["BTCUSDT"].entry_price == Dec("82200") * Dec("1.0005")


def test_a_jump_past_the_limit_fills_only_when_the_price_comes_back_to_the_limit():
    sim, _, clock, _ = placed()
    sim.on_candle("BTCUSDT", candle(clock(), "83000", "84000", "82600", "83500"))
    assert not sim.positions and sim.waiting["BTCUSDT"].triggered                 # no chasing
    sim.on_candle("BTCUSDT", candle(clock() + H4, "83500", "83600", "82300", "82500"))
    assert sim.positions["BTCUSDT"].entry_price == Dec("82410") * Dec("1.0005")


def test_on_the_fill_candle_only_the_stop_is_checked():
    sim, _, clock, _ = placed()
    t = sim.on_candle("BTCUSDT", candle(clock(), "81000", "92000", "78500", "80000"))
    assert t is not None and t.exit_reason == fills.STOP_LOSS and not sim.positions   # cautious: stop, never target
    sim2, _, c2, _ = placed()
    assert sim2.on_candle("BTCUSDT", candle(c2(), "81000", "92000", "80000", "91500")) is None
    assert "BTCUSDT" in sim2.positions                                            # target not taken on the fill candle


def test_expires_at_its_time_and_ignores_candles_from_before_it():
    sim, _, clock, _ = placed()
    assert sim.on_candle("BTCUSDT", candle(clock() - H4, "81000", "90000", "80000", "85000")) is None
    assert "BTCUSDT" in sim.waiting
    sim.on_candle("BTCUSDT", candle(clock() + DAY, "81000", "90000", "80000", "85000"))
    assert not sim.waiting and not sim.positions and sim.waiting_log["expired"] == 1


def test_one_order_per_coin_a_new_one_replaces_it_and_expired_ones_go_first():
    sim, _, clock, _ = placed()
    r = sim.submit(advance(clock, trigger_price=Dec("82100"), entry_price=Dec("82510")))
    assert r.waiting and sim.waiting["BTCUSDT"].trigger == Dec("82100") and sim.waiting_log["replaced"] == 1
    clock.advance(DAY)
    sim.submit(advance(clock))
    assert sim.waiting_log["expired"] == 1 and sim.waiting_log["placed"] == 3


def test_every_waiting_order_is_cancelled_when_entries_are_blocked():
    sim, _, clock, _ = placed()
    sim.modes.block_entries("sultan", "test of the block")
    sim.account_state()
    assert not sim.waiting and sim.waiting_log["cancelled (entries blocked)"] == 1


def test_the_risk_engine_refuses_a_malformed_advance_order():
    sim, _, clock = make()
    for bad in (dict(trigger_price=Dec("83000")),                      # trigger above its limit
                dict(trigger_price=Dec("78000")),                      # trigger below the stop
                dict(valid_until=None)):                               # no expiry
        r = sim.submit(advance(clock, **bad))
        assert not r.waiting and r.decision.code is RejectCode.EXIT_PLAN, bad
    assert not sim.waiting


def test_an_intent_without_a_trigger_still_fills_at_once():
    sim, _, clock = make()
    intent = replace(advance(clock), trigger_price=None, valid_until=None, entry_price=Dec("80100"),
                     take_profit_price=Dec("90000"))
    r = sim.submit(intent)
    assert r.filled and not r.waiting and not sim.waiting


def test_the_fill_goes_through_submit_and_a_fresh_risk_check(monkeypatch):
    import bot.paper.sim as sim_mod
    seen, real = [], sim_mod.evaluate

    def spy(intent, state, *a, **k):
        seen.append((intent.trigger_price, len(state.positions)))
        return real(intent, state, *a, **k)
    monkeypatch.setattr(sim_mod, "evaluate", spy)
    sim, _, clock, _ = placed()
    sim.on_candle("BTCUSDT", candle(clock(), "81000", "82500", "80800", "82300"))
    assert seen == [(Dec("82000"), 0), (None, 0)]          # placed, then checked again at the fill (not counted twice)
    assert "BTCUSDT" in sim.positions


def test_a_refusal_at_fill_time_means_no_buy(monkeypatch):
    import bot.paper.sim as sim_mod
    from bot.risk.models import Decision
    sim, _, clock, _ = placed()
    monkeypatch.setattr(sim_mod, "evaluate", lambda *a, **k: Decision.reject(RejectCode.DAILY_LOSS, "limit hit"))
    sim.on_candle("BTCUSDT", candle(clock(), "81000", "82500", "80800", "82300"))
    assert not sim.positions and not sim.waiting and sim.quote == Dec("10000")
    assert sim.waiting_log["not filled at the trigger (REJ_DAILY_LOSS_LIMIT)"] == 1


def test_only_the_simulator_itself_can_pass_a_fill_price():
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "bot"
    for f in src.rglob("*.py"):
        if f.name == "sim.py" and f.parent.name == "paper":
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.keyword):
                assert n.arg != "_triggered_at", f
