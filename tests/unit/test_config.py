from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot.core.config import (
    LIVE_CONFIRM_ENV, LIVE_CONFIRM_PHRASE, AppConfig, Environment, LiveTradingNotConfirmed,
    RiskConfig, assert_live_allowed, load_config,
)

ROOT = Path(__file__).resolve().parents[2]


def test_default_yaml_loads_and_is_safe():
    cfg = load_config(ROOT / "config" / "default.yaml")
    assert cfg.environment is Environment.PAPER and cfg.live_trading is False
    assert cfg.risk.risk_per_trade_pct == Dec("0.25")


@pytest.mark.parametrize("field,value", [
    ("risk_per_trade_pct", "0.6"), ("daily_loss_pct", "2"), ("weekly_loss_pct", "3.5"),
    ("max_open_positions", 4), ("max_open_risk_pct", "2"), ("max_invested_pct", "60"),
    ("max_coin_exposure_pct", "30"), ("drawdown_breaker_pct", "15"), ("max_clock_skew_ms", 2000),
])
def test_T_RISK_030_values_above_ceiling_are_refused(field, value):
    with pytest.raises(ValueError, match="hard ceiling"):
        RiskConfig(**{field: value})


def test_tighter_values_are_accepted():
    r = RiskConfig(risk_per_trade_pct="0.1", daily_loss_pct="1", max_open_positions=1)
    assert r.max_open_positions == 1


def test_reward_risk_floor():
    with pytest.raises(ValueError, match="hard floor"):
        RiskConfig(min_reward_risk="0.8")


def test_unknown_keys_are_refused():
    with pytest.raises(ValueError):
        RiskConfig(allow_leverage=True)


def test_only_usdt_spot_symbols():
    with pytest.raises(ValueError):
        AppConfig(universe=["BTCUSDT", "ETHBTC"])


def test_T_MODE_001_live_flag_needs_live_environment():
    with pytest.raises(ValueError):
        AppConfig(environment="paper", live_trading=True)


def test_T_MODE_002_live_needs_typed_confirmation():
    cfg = AppConfig(environment="live", live_trading=True)
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={})
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(cfg, environ={LIVE_CONFIRM_ENV: "yes"})
    assert_live_allowed(cfg, environ={LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE})


def test_T_MODE_003_paper_is_never_live():
    with pytest.raises(LiveTradingNotConfirmed):
        assert_live_allowed(AppConfig(), environ={LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE})
