"""Configuration with hard safety ceilings.

The YAML config can tighten any risk limit, but a value looser than its hard
ceiling is refused at startup. The ceilings live here, in code, so they cannot
be raised from a config file, an environment variable or the dashboard.
"""
from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---- Hard ceilings (design package section 4, "Limits" table) -----------------
# Percent values are percent of equity, as written in the spec.
CEILINGS: dict[str, Decimal] = {
    "risk_per_trade_pct": Decimal("0.5"),       # R-01
    "daily_loss_pct": Decimal("1.5"),           # R-02
    "weekly_loss_pct": Decimal("3"),            # R-03
    "max_open_positions": Decimal("3"),         # R-04
    "max_open_risk_pct": Decimal("1.5"),        # R-05
    "max_invested_pct": Decimal("50"),          # R-06
    "max_coin_exposure_pct": Decimal("25"),     # R-07
    "max_per_corr_group": Decimal("2"),         # R-08
    "drawdown_breaker_pct": Decimal("10"),      # R-09
    "max_clock_skew_ms": Decimal("1000"),       # R-13
}
FLOORS: dict[str, Decimal] = {
    "min_reward_risk": Decimal("1.0"),          # R-10
}

# The most USDT the bot may ever trade with on the real exchange (bot live start --capital). In code, like the
# ceilings above, so it cannot be raised from a file. Sultan chose to start small (23 Oct 2026).
LIVE_CAPITAL_CEILING_USDT = Decimal("1000")
LIVE_CONFIRM_ENV = "BOT_LIVE_CONFIRM"
LIVE_CONFIRM_PHRASE = "I accept live trading risk"


class Environment(str, Enum):
    SIM = "sim"
    PAPER = "paper"
    TESTNET = "testnet"
    DEMO = "demo"
    LIVE = "live"


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_per_trade_pct: Decimal = Decimal("0.25")
    daily_loss_pct: Decimal = Decimal("1.5")
    weekly_loss_pct: Decimal = Decimal("3")
    max_open_positions: int = 3
    max_open_risk_pct: Decimal = Decimal("1.5")
    max_invested_pct: Decimal = Decimal("50")
    max_coin_exposure_pct: Decimal = Decimal("25")
    corr_threshold: Decimal = Decimal("0.8")
    max_per_corr_group: int = 2
    drawdown_breaker_pct: Decimal = Decimal("6")
    min_reward_risk: Decimal = Decimal("1.5")
    max_spread_pct: Decimal = Decimal("0.10")
    min_quote_volume_24h: Decimal = Decimal("20000000")
    min_depth_multiple: Decimal = Decimal("5")
    max_data_age_s: Decimal = Decimal("10")
    max_clock_skew_ms: int = 500
    entry_slippage_pct: Decimal = Decimal("0.05")
    default_stop_slippage_pct: Decimal = Decimal("1.0")
    stop_slippage_pct: dict[str, Decimal] = Field(
        default_factory=lambda: {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("1.0")}
    )
    auto_clear_daily_block: bool = False

    @model_validator(mode="after")
    def _check_ceilings(self) -> "RiskConfig":
        errors = []
        for name, ceiling in CEILINGS.items():
            value = Decimal(getattr(self, name))
            if value > ceiling:
                errors.append(f"{name}={value} is above the hard ceiling {ceiling}")
            if value <= 0:
                errors.append(f"{name} must be positive")
        for name, floor in FLOORS.items():
            if Decimal(getattr(self, name)) < floor:
                errors.append(f"{name} is below the hard floor {floor}")
        if not Decimal("0") < self.corr_threshold < Decimal("1"):
            errors.append("corr_threshold must be between 0 and 1")
        for name in ("risk_per_trade_pct", "max_spread_pct", "entry_slippage_pct", "default_stop_slippage_pct"):
            if Decimal(getattr(self, name)) <= 0:
                errors.append(f"{name} must be positive")
        if any(v <= 0 for v in self.stop_slippage_pct.values()):
            errors.append("stop_slippage_pct values must be positive")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def stop_slippage_for(self, symbol: str) -> Decimal:
        return self.stop_slippage_pct.get(symbol, self.default_stop_slippage_pct)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Environment = Environment.PAPER
    live_trading: bool = False
    universe: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    quote_asset: Literal["USDT"] = "USDT"
    day_boundary_utc_hour: int = 0
    risk: RiskConfig = Field(default_factory=RiskConfig)

    @field_validator("universe")
    @classmethod
    def _usdt_spot_only(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("universe must not be empty")
        bad = [s for s in v if not s.endswith("USDT") or not s.isupper()]
        if bad:
            raise ValueError(f"only USDT-quoted symbols are allowed: {bad}")
        return v

    @field_validator("day_boundary_utc_hour")
    @classmethod
    def _hour(cls, v: int) -> int:
        if v != 0:
            raise ValueError("only a 00:00 UTC day boundary is supported in this version")
        return v

    @model_validator(mode="after")
    def _live_guard(self) -> "AppConfig":
        if self.live_trading and self.environment is not Environment.LIVE:
            raise ValueError("live_trading may only be true when environment is 'live'")
        return self


class LiveTradingNotConfirmed(RuntimeError):
    pass


def assert_live_allowed(cfg: AppConfig, environ: dict[str, str] | None = None) -> None:
    """Live trading needs the config flag AND a typed confirmation in the environment."""
    environ = os.environ if environ is None else environ
    if cfg.environment is not Environment.LIVE or not cfg.live_trading:
        raise LiveTradingNotConfirmed("live trading is disabled in the config")
    if environ.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_PHRASE:
        raise LiveTradingNotConfirmed(
            f"set {LIVE_CONFIRM_ENV} to the exact confirmation phrase to enable live trading"
        )


def load_config(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return AppConfig.model_validate(data)
