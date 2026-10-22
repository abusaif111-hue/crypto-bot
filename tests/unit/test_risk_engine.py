from dataclasses import replace
from decimal import Decimal as Dec

import pytest

from bot.core.config import AppConfig, RiskConfig
from bot.exchange.filters import parse_symbol
from bot.risk.engine import evaluate, position_open_risk
from bot.risk.models import Mode, OpenPosition, RejectCode
from tests.conftest import exchange_info_symbol


def run(intent, state, market, rules, cfg, group="majors"):
    return evaluate(intent, state, market, rules, cfg, group)


def test_T_RISK_001_worked_example(intent, state, market, btc_rules, cfg):
    """Hand-checked against the sizing formula in the design package.

    pe = 80000 x 1.0005 = 80040; ps = 78000 x 0.995 = 77610
    loss/unit = 2430 + 0.001 x 157650 = 2587.65
    budget = 0.25% x 10000 = 25 USDT -> 25 / 2587.65 = 0.009661... -> 0.00966
    """
    d = run(intent, state, market, btc_rules, cfg)
    assert d.accepted, d.reason
    assert d.qty == Dec("0.00966")
    assert d.planned_risk == Dec("0.00966") * Dec("2587.65")
    assert d.planned_risk <= Dec("25")
    assert d.details["budget_limited_by"] == "per_trade"
    assert round(d.reward_risk, 2) == Dec("2.24")


def test_T_RISK_040_exit_plan_required(intent, state, market, btc_rules, cfg):
    for bad in (replace(intent, stop_price=Dec("81000")), replace(intent, take_profit_price=Dec("79000")),
                replace(intent, horizon_hours=0), replace(intent, invalidation=" ")):
        d = run(bad, state, market, btc_rules, cfg)
        assert not d.accepted and d.code is RejectCode.EXIT_PLAN


def test_long_only(intent, state, market, btc_rules, cfg):
    d = run(replace(intent, side="SELL"), state, market, btc_rules, cfg)
    assert d.code is RejectCode.SIDE


@pytest.mark.parametrize("mode", [Mode.ENTRIES_BLOCKED, Mode.PAUSED, Mode.EMERGENCY_FLATTEN])
def test_T_BRK_001_no_entries_unless_running(intent, state, market, btc_rules, cfg, mode):
    assert run(intent, replace(state, mode=mode), market, btc_rules, cfg).code is RejectCode.MODE


def test_T_FAIL_001_stale_data_blocks(intent, state, market, btc_rules, cfg):
    assert run(intent, state, replace(market, data_age_s=Dec("11")), btc_rules, cfg).code is RejectCode.STALE_DATA


def test_T_FAIL_002_clock_skew_blocks(intent, state, market, btc_rules, cfg):
    assert run(intent, state, replace(market, clock_skew_ms=900), btc_rules, cfg).code is RejectCode.CLOCK


def test_wide_spread_blocks(intent, state, market, btc_rules, cfg):
    assert run(intent, state, replace(market, best_bid=Dec("79800")), btc_rules, cfg).code is RejectCode.MARKET_QUALITY


def test_not_in_universe(intent, state, market, cfg):
    rules = parse_symbol(exchange_info_symbol("DOGEUSDT", tick="0.00001", step="1"))
    assert run(replace(intent, symbol="DOGEUSDT"), state, market, rules, cfg).code is RejectCode.UNIVERSE


def test_halted_symbol(intent, state, market, cfg):
    rules = parse_symbol(exchange_info_symbol(status="BREAK"))
    assert run(intent, state, market, rules, cfg).code is RejectCode.SYMBOL


def pos(symbol="ETHUSDT", group="majors", qty="0.4", entry="2500", stop="2400", bid="2500"):
    return OpenPosition(symbol=symbol, qty=Dec(qty), avg_entry=Dec(entry), stop_price=Dec(stop),
                        current_bid=Dec(bid), corr_group=group)


def test_R14_no_adding_to_a_position(intent, state, market, btc_rules, cfg):
    s = replace(state, positions=(pos(symbol="BTCUSDT"),))
    assert run(intent, s, market, btc_rules, cfg).code is RejectCode.EXISTING_POSITION


def test_R04_max_open_positions(intent, state, market, btc_rules, cfg):
    s = replace(state, positions=(pos("ETHUSDT", "a"), pos("SOLUSDT", "b"), pos("BNBUSDT", "c")))
    assert run(intent, s, market, btc_rules, cfg).code is RejectCode.MAX_POSITIONS


def test_R08_correlation_group_full(intent, state, market, btc_rules, cfg):
    s = replace(state, positions=(pos("ETHUSDT", "majors"), pos("SOLUSDT", "majors")))
    assert run(intent, s, market, btc_rules, cfg, "majors").code is RejectCode.CORRELATION


def test_T_BRK_002_daily_loss_limit_blocks(intent, state, market, btc_rules, cfg):
    s = replace(state, daily_pnl=Dec("-150"), weekly_pnl=Dec("-150"), equity=Dec("9850"))
    assert run(intent, s, market, btc_rules, cfg).code is RejectCode.DAILY_LOSS


def test_T_BRK_003_weekly_loss_limit_blocks(intent, state, market, btc_rules, cfg):
    s = replace(state, daily_pnl=Dec("0"), weekly_pnl=Dec("-300"), equity=Dec("9700"))
    assert run(intent, s, market, btc_rules, cfg).code is RejectCode.WEEKLY_LOSS


def test_T_BRK_004_drawdown_breaker_blocks(intent, state, market, btc_rules, cfg):
    assert run(intent, replace(state, drawdown_pct=Dec("6")), market, btc_rules, cfg).code is RejectCode.DRAWDOWN


def test_T_RISK_002_daily_budget_counts_open_risk(intent, state, market, btc_rules, cfg):
    """Loss so far + risk still open at the stops + the new trade must fit inside 1.5% of day start."""
    s = replace(state, daily_pnl=Dec("-130"), weekly_pnl=Dec("-130"), equity=Dec("9870"), free_quote=Dec("9870"))
    d = run(intent, s, market, btc_rules, cfg)
    assert d.accepted and d.details["budget_limited_by"] == "daily"
    assert Dec("130") + d.planned_risk <= Dec("150")


def test_T_RISK_003_no_budget_left(intent, state, market, btc_rules, cfg):
    # Two big open positions whose stops already use the whole open-risk budget
    big = (pos("ETHUSDT", "a", qty="1.5", stop="2400", bid="2500"), pos("SOLUSDT", "b", qty="1", entry="200", stop="100", bid="200"))
    s = replace(state, positions=big)
    d = run(intent, s, market, btc_rules, cfg)
    assert d.code is RejectCode.RISK_BUDGET


def test_R10_reward_risk_too_low(intent, state, market, btc_rules, cfg):
    d = run(replace(intent, take_profit_price=Dec("82000")), state, market, btc_rules, cfg)
    assert d.code is RejectCode.REWARD_RISK


def test_T_SIZE_010_too_small_account_is_skipped_not_upsized(intent, market, btc_rules, cfg, state):
    s = replace(state, equity=Dec("50"), free_quote=Dec("50"), day_start_equity=Dec("50"), week_start_equity=Dec("50"))
    d = run(intent, s, market, btc_rules, cfg)
    assert not d.accepted and d.code is RejectCode.EXCHANGE_MIN


def test_size_capped_by_cash(intent, state, market, btc_rules):
    cfg = AppConfig(risk=RiskConfig(risk_per_trade_pct="0.5"))
    s = replace(state, free_quote=Dec("300"))
    d = run(replace(intent, stop_price=Dec("79600"), take_profit_price=Dec("82000")), s, market, btc_rules, cfg)
    assert d.accepted and d.details["size_limited_by"] == "cash"
    assert d.qty * Dec("80040") * Dec("1.001") <= Dec("300")


def test_open_risk_measured_from_current_bid():
    p = pos(qty="1", entry="2500", stop="2400", bid="2600")
    r = position_open_risk(p, Dec("0.01"), Dec("0.001"))
    assert r == Dec("2600") - Dec("2376") + Dec("2.376")
    gapped = position_open_risk(pos(qty="1", stop="2400", bid="2300"), Dec("0.01"), Dec("0.001"))
    assert gapped == Dec("2.376")  # already through the stop: only the exit fee remains as planned risk
