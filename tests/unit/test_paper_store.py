"""Module 3: the paper account survives restarts, and a restart never clears a breaker."""
import json
from decimal import Decimal as Dec

import pytest

from bot.core.config import AppConfig
from bot.paper import fills, store
from bot.paper.store import StateError
from bot.risk.models import Mode, RejectCode
from tests.fakes import Clock, FakeMarket
from tests.unit.test_sim_exchange import INTENT, make


def restore(path, market=None, clock=None):
    return store.load_or_create(path, AppConfig(), market or FakeMarket(), clock or Clock())


def test_missing_file_starts_a_new_10000_usdt_account(tmp_path):
    sim, restored = restore(tmp_path / "state.json")
    assert not restored and sim.quote == Dec("10000") and not sim.positions


def test_round_trip_keeps_everything(tmp_path):
    sim, market, clock = make()
    assert sim.submit(INTENT).filled
    market.set_bid("BTCUSDT", "86000")
    sim.poll()
    clock.advance(60)
    market.set_bid("BTCUSDT", "79990")
    assert sim.submit(INTENT).filled                    # one closed trade, one open position
    path = tmp_path / "state.json"
    store.save(sim, path, clock())
    assert not (tmp_path / "state.json.tmp").exists()

    back, restored = restore(path, market, clock)
    assert restored
    assert back.quote == sim.quote and back.created_at == sim.created_at
    assert back.positions == sim.positions and back.fills == sim.fills and back.trades == sim.trades
    assert back.equity() == sim.equity()
    assert (back.index.units, back.index.peak_value_per_unit) == (sim.index.units, sim.index.peak_value_per_unit)
    assert back.periods.day_anchor == sim.periods.day_anchor
    assert back.periods.daily_pnl(back.equity()) == sim.periods.daily_pnl(sim.equity())
    assert back.fee_rate == Dec("0.001") and back.starting_quote == Dec("10000")


def test_restart_blocks_entries_until_an_operator_resumes(tmp_path):
    sim, market, clock = make()
    assert sim.modes.mode is Mode.RUNNING
    store.save(sim, tmp_path / "s.json", clock())
    back, _ = restore(tmp_path / "s.json", market, clock)
    assert back.modes.mode is Mode.ENTRIES_BLOCKED
    assert back.modes.history[-1].reason.startswith("restarted")
    assert back.submit(INTENT).decision.code is RejectCode.MODE
    back.resume("sultan", "checked after the restart")
    assert back.submit(INTENT).filled


def test_restart_never_clears_a_breaker(tmp_path):
    sim, market, clock = make()
    sim.quote -= Dec("700")                              # 7% drawdown trips the 6% breaker
    sim.account_state()
    store.save(sim, tmp_path / "s.json", clock())
    back, _ = restore(tmp_path / "s.json", market, clock)
    assert RejectCode.DRAWDOWN.value in back.modes.active_breaches
    assert back.index.drawdown_pct >= Dec("6")
    with pytest.raises(PermissionError):
        back.resume("sultan", "trying to resume after restart")


def test_open_position_keeps_its_exits_after_a_restart(tmp_path):
    sim, market, clock = make()
    assert sim.submit(INTENT).filled
    store.save(sim, tmp_path / "s.json", clock())
    back, _ = restore(tmp_path / "s.json", market, clock)
    market.set_bid("BTCUSDT", "78000")
    (t,) = back.poll()
    assert t.exit_reason == fills.STOP_LOSS and t.exit_price == Dec("77610")
    assert back.quote == Dec("10000") + t.pnl


@pytest.mark.parametrize("content", ["", "{not json", json.dumps({"version": 99})])
def test_unreadable_file_stops_instead_of_resetting(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(StateError):
        restore(path)


def test_inconsistent_file_is_refused(tmp_path):
    sim, market, clock = make()
    assert sim.submit(INTENT).filled
    store.save(sim, tmp_path / "s.json", clock())
    data = json.loads((tmp_path / "s.json").read_text())
    data["bids"] = {}
    (tmp_path / "s.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="no saved price"):
        restore(tmp_path / "s.json")
    data = json.loads((tmp_path / "s.json").read_text())
    data["bids"] = {"BTCUSDT": "79990"}
    data["quote"] = "-1"
    (tmp_path / "s.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="negative"):
        restore(tmp_path / "s.json")
