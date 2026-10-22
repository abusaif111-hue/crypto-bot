"""Module 3: whatever the price path, the paper exchange keeps its books straight.

Hypothesis drives random trade plans and random bid paths (gaps included) through
SimExchange and checks, after every step: no negative balances, every fill pays
exactly the taker fee, equity = cash + coins at bid, and when flat, cash =
start + the sum of trade P&L. A stop that did not gap never loses more than the
risk engine planned.
"""
from decimal import Decimal as Dec

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bot.core.config import AppConfig
from bot.paper import fills
from bot.paper.sim import SimExchange
from bot.risk.models import TradeIntent
from tests.fakes import Clock, FakeMarket


def dec(lo, hi, places=2):
    return st.decimals(min_value=Dec(str(lo)), max_value=Dec(str(hi)), places=places, allow_nan=False,
                       allow_infinity=False)


@st.composite
def plan(draw):
    entry = draw(dec("30000", "120000", 0))
    stop = entry * (1 - draw(dec("0.005", "0.08", 3)))
    tp = entry * (1 + draw(dec("0.02", "0.25", 3)))
    # bid path as moves relative to entry, with the exact stop and target levels mixed in
    moves = draw(st.lists(st.one_of(dec("-0.15", "0.30", 4), st.sampled_from(["stop", "tp"])), min_size=1, max_size=12))
    hours = draw(st.integers(1, 6))
    return entry, stop.quantize(Dec("0.01")), tp.quantize(Dec("0.01")), moves, hours


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(plans=st.lists(plan(), min_size=1, max_size=4))
def test_paper_exchange_invariants(plans):
    clock, market = Clock(), FakeMarket()
    sim = SimExchange(AppConfig(), market, clock=clock)
    sim.resume("sultan", "property test session")
    for entry, stop, tp, moves, hours in plans:
        market.set_bid("BTCUSDT", entry - 10)
        market.avg["BTCUSDT"] = entry
        intent = TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=entry, stop_price=stop, take_profit_price=tp,
                             strategy="prop", horizon_hours=hours, invalidation="test")
        sim.submit(intent)
        for m in moves:
            clock.advance(3600)
            bid = stop if m == "stop" else tp if m == "tp" else (entry * (1 + m)).quantize(Dec("0.01"))
            market.set_bid("BTCUSDT", max(bid, Dec("1")))
            sim.poll()
            check(sim)
        if sim.positions:                                  # finish the trade with a time stop
            clock.advance(hours * 3600)
            sim.poll()
            check(sim)
    assert not sim.positions
    assert sim.quote == Dec("10000") + sum((t.pnl for t in sim.trades), Dec(0))


def check(sim: SimExchange) -> None:
    assert sim.quote >= 0
    assert all(p.qty > 0 for p in sim.positions.values())
    for f in sim.fills:
        assert f.fee == f.qty * f.price * Dec("0.001")
    assert sim.equity() == sim.quote + sum((p.qty * sim._bids[p.symbol] for p in sim.positions.values()), Dec(0))
    for t in sim.trades:
        assert t.pnl == t.qty * (t.exit_price - t.entry_price) - t.entry_fee - t.exit_fee
        if t.exit_reason == fills.STOP_LOSS and not t.gapped:
            assert -t.pnl <= t.planned_risk, (t.pnl, t.planned_risk)
        if t.exit_reason == fills.TAKE_PROFIT:
            assert t.pnl > 0
