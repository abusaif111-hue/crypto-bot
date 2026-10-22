"""Inputs and outputs of the risk engine. Plain immutable data, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class Mode(str, Enum):
    RUNNING = "RUNNING"
    ENTRIES_BLOCKED = "ENTRIES_BLOCKED"
    PAUSED = "PAUSED"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


class RejectCode(str, Enum):
    MODE = "REJ_MODE"
    SIDE = "REJ_LONG_ONLY"
    EXIT_PLAN = "REJ_EXIT_PLAN"
    SYMBOL = "REJ_SYMBOL_NOT_TRADEABLE"
    UNIVERSE = "REJ_NOT_IN_UNIVERSE"
    STALE_DATA = "REJ_STALE_DATA"
    CLOCK = "REJ_CLOCK_SKEW"
    MARKET_QUALITY = "REJ_MARKET_QUALITY"
    EXISTING_POSITION = "REJ_NO_ADDING"
    MAX_POSITIONS = "REJ_MAX_POSITIONS"
    DAILY_LOSS = "REJ_DAILY_LOSS_LIMIT"
    WEEKLY_LOSS = "REJ_WEEKLY_LOSS_LIMIT"
    DRAWDOWN = "REJ_DRAWDOWN_BREAKER"
    CORRELATION = "REJ_CORRELATION_GROUP"
    REWARD_RISK = "REJ_REWARD_RISK"
    RISK_BUDGET = "REJ_NO_RISK_BUDGET"
    EXCHANGE_MIN = "REJ_BELOW_EXCHANGE_MIN"
    BAD_INPUT = "REJ_BAD_INPUT"


@dataclass(frozen=True)
class TradeIntent:
    """A strategy's proposal. Every field of the exit plan is mandatory."""
    symbol: str
    side: str                      # only "BUY" is accepted (long-only Spot)
    entry_price: Decimal           # planned limit price
    stop_price: Decimal
    take_profit_price: Decimal
    strategy: str
    horizon_hours: int             # time stop
    invalidation: str              # plain-language condition that voids the trade


@dataclass(frozen=True)
class MarketSnapshot:
    best_bid: Decimal
    best_ask: Decimal
    quote_volume_24h: Decimal
    ask_depth_quote: Decimal       # USDT available on the ask side near the touch
    data_age_s: Decimal            # age of the newest price update
    clock_skew_ms: int             # |local clock - Binance server time|
    reference_price: Decimal | None = None


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    qty: Decimal
    avg_entry: Decimal
    stop_price: Decimal
    current_bid: Decimal
    corr_group: str


@dataclass(frozen=True)
class AccountState:
    mode: Mode
    equity: Decimal                # valued at best bid, see portfolio.accounting
    free_quote: Decimal            # free USDT
    day_start_equity: Decimal
    week_start_equity: Decimal
    daily_pnl: Decimal             # net of fees, external flows excluded
    weekly_pnl: Decimal
    drawdown_pct: Decimal          # from the unit-value index peak, positive number
    positions: tuple[OpenPosition, ...] = ()
    taker_fee_rate: Decimal = Decimal("0.001")   # fraction, from GET /api/v3/account/commission


@dataclass(frozen=True)
class Decision:
    accepted: bool
    qty: Decimal = Decimal("0")
    code: RejectCode | None = None
    reason: str = ""
    planned_risk: Decimal = Decimal("0")   # USDT lost if the stop fills at its slippage allowance, fees included
    reward_risk: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def reject(code: RejectCode, reason: str, **details: Any) -> "Decision":
        return Decision(accepted=False, code=code, reason=reason, details=details)
