from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as Dec

import pytest

from bot.core.config import AppConfig, RiskConfig
from bot.portfolio.accounting import Balance, EquityIndex, MissingPrice, PeriodTracker, value_equity
from bot.risk.models import Mode
from bot.risk.modes import EMERGENCY_CONFIRM_PHRASE, ModeController


def t(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


# ---------------------------------------------------------------- modes
def test_T_MODE_004_starts_blocked(cfg):
    assert ModeController(cfg).mode is Mode.ENTRIES_BLOCKED


def test_T_BRK_005_daily_breach_blocks_and_needs_reason(cfg, state):
    mc = ModeController(cfg)
    mc.resume("sultan", "pre-open checks passed", state)
    assert mc.entries_allowed
    hit = replace(state, daily_pnl=Dec("-160"), equity=Dec("9840"))
    assert mc.on_state(hit) == ["REJ_DAILY_LOSS_LIMIT"]
    assert mc.mode is Mode.ENTRIES_BLOCKED and not mc.entries_allowed
    with pytest.raises(PermissionError):
        mc.resume("sultan", "ok", hit)                         # reason too short
    with pytest.raises(PermissionError):
        mc.resume("sultan", "I want to keep trading", hit)     # breach still active
    recovered = replace(hit, daily_pnl=Dec("-10"))
    with pytest.raises(PermissionError):
        mc.resume("sultan", "price came back today", recovered)  # daily breach holds until the next day
    mc.on_day_rollover(replace(state, daily_pnl=Dec("0")))
    mc.resume("sultan", "new day, reviewed yesterday's losses", state)
    assert mc.mode is Mode.RUNNING


def test_T_BRK_006_weekly_breach_survives_day_rollover(cfg, state):
    mc = ModeController(cfg)
    mc.resume("op", "pre-open checks passed", state)
    hit = replace(state, weekly_pnl=Dec("-310"))
    mc.on_state(hit)
    mc.on_day_rollover(hit)
    with pytest.raises(PermissionError):
        mc.resume("op", "trying again after weekly breach", hit)
    assert mc.mode is Mode.ENTRIES_BLOCKED


def test_T_BRK_007_drawdown_needs_review(cfg, state):
    mc = ModeController(cfg)
    mc.resume("op", "pre-open checks passed", state)
    mc.on_state(replace(state, drawdown_pct=Dec("6.5")))
    ok = replace(state, drawdown_pct=Dec("4"))
    with pytest.raises(PermissionError):
        mc.resume("op", "drawdown recovered somewhat", ok)
    mc.complete_drawdown_review("op", "reviewed all trades in the drawdown", ok)
    mc.resume("op", "review signed off, resuming", ok)
    assert mc.mode is Mode.RUNNING


def test_T_BRK_008_auto_clear_daily_only_when_enabled(state):
    cfg = AppConfig(risk=RiskConfig(auto_clear_daily_block=True))
    mc = ModeController(cfg)
    mc.resume("op", "pre-open checks passed", state)
    mc.on_state(replace(state, daily_pnl=Dec("-200")))
    mc.on_day_rollover(state)
    assert mc.mode is Mode.RUNNING


def test_T_FAIL_010_faults_block_until_cleared(cfg, state):
    mc = ModeController(cfg)
    mc.resume("op", "pre-open checks passed", state)
    mc.report_fault("UNPROTECTED_POSITION", "BTCUSDT")
    assert mc.mode is Mode.ENTRIES_BLOCKED
    with pytest.raises(PermissionError):
        mc.resume("op", "want to continue now", state)
    mc.clear_fault("UNPROTECTED_POSITION")
    mc.resume("op", "protection restored and verified", state)
    assert mc.entries_allowed


def test_T_EMG_001_emergency_flatten_needs_exact_phrase(cfg, state):
    mc = ModeController(cfg)
    with pytest.raises(PermissionError):
        mc.emergency_flatten("op", "yes")
    mc.emergency_flatten("op", EMERGENCY_CONFIRM_PHRASE)
    assert mc.mode is Mode.EMERGENCY_FLATTEN
    with pytest.raises(PermissionError):
        mc.resume("op", "trying to resume after flatten", state)


def test_T_AUD_001_every_transition_is_recorded(cfg, state):
    mc = ModeController(cfg)
    mc.resume("op", "pre-open checks passed", state)
    mc.pause("op", "maintenance window")
    assert [(h.from_mode, h.to_mode, h.actor) for h in mc.history] == [
        (Mode.ENTRIES_BLOCKED, Mode.RUNNING, "op"), (Mode.RUNNING, Mode.PAUSED, "op")]


# ---------------------------------------------------------------- accounting
def test_equity_valued_at_bid():
    eq = value_equity([Balance("USDT", Dec("5000"), Dec("100")), Balance("BTC", Dec("0.01"), Dec("0.02"))],
                      {"BTCUSDT": Dec("80000")})
    assert eq == Dec("5100") + Dec("0.03") * Dec("80000")


def test_missing_price_is_an_error_not_zero():
    with pytest.raises(MissingPrice):
        value_equity([Balance("ETH", Dec("1"), Dec("0"))], {})


def test_deposits_do_not_change_drawdown():
    idx = EquityIndex()
    idx.start(Dec("10000"))
    idx.mark(Dec("9000"))
    assert idx.drawdown_pct == Dec("10")
    idx.flow(Dec("5000"), equity_before_flow=Dec("9000"))
    idx.mark(Dec("14000"))
    assert idx.drawdown_pct == Dec("10")


def test_withdrawal_does_not_look_like_a_loss():
    idx = EquityIndex()
    idx.start(Dec("10000"))
    idx.flow(Dec("-4000"), equity_before_flow=Dec("10000"))
    idx.mark(Dec("6000"))
    assert idx.drawdown_pct == 0


def test_period_tracker_excludes_flows_and_rolls_over():
    pt = PeriodTracker()
    assert pt.update(t(19, 1), Dec("10000")) == ["day", "week"]     # Monday 19 Oct 2026
    pt.flow(Dec("1000"))
    assert pt.daily_pnl(Dec("10900")) == Dec("-100")
    assert pt.update(t(20, 0), Dec("10900")) == ["day"]
    assert pt.daily_pnl(Dec("10900")) == 0 and pt.weekly_pnl(Dec("10900")) == Dec("-100")
    assert pt.update(t(26, 0), Dec("11000")) == ["day", "week"]
