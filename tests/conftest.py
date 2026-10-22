from __future__ import annotations

from decimal import Decimal as Dec

import pytest

from bot.core.config import AppConfig
from bot.exchange.filters import SymbolRules, parse_symbol
from bot.risk.models import AccountState, MarketSnapshot, Mode, TradeIntent


def exchange_info_symbol(symbol: str = "BTCUSDT", tick: str = "0.01", step: str = "0.00001",
                         min_notional: str = "5", status: str = "TRADING") -> dict:
    """Shape copied from GET /api/v3/exchangeInfo (values are illustrative)."""
    return {
        "symbol": symbol, "status": status, "baseAsset": symbol[:-4], "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"],
        "ocoAllowed": True, "otoAllowed": True, "opoAllowed": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000.00", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "9000.00000", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000", "maxQty": "100.0", "stepSize": "0.00000"},
            {"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True,
             "maxNotional": "9000000.00", "applyMaxToMarket": False, "avgPriceMins": 5},
            {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "5", "bidMultiplierDown": "0.2",
             "askMultiplierUp": "5", "askMultiplierDown": "0.2", "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
        ],
    }


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


@pytest.fixture
def btc_rules() -> SymbolRules:
    return parse_symbol(exchange_info_symbol())


@pytest.fixture
def intent() -> TradeIntent:
    return TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=Dec("80000"), stop_price=Dec("78000"),
                       take_profit_price=Dec("86000"), strategy="H1-trend", horizon_hours=96,
                       invalidation="daily close back below the breakout level")


@pytest.fixture
def market() -> MarketSnapshot:
    return MarketSnapshot(best_bid=Dec("79990"), best_ask=Dec("80000"), quote_volume_24h=Dec("1500000000"),
                          ask_depth_quote=Dec("2000000"), data_age_s=Dec("1"), clock_skew_ms=20,
                          reference_price=Dec("80000"))


@pytest.fixture
def state() -> AccountState:
    return AccountState(mode=Mode.RUNNING, equity=Dec("10000"), free_quote=Dec("10000"),
                        day_start_equity=Dec("10000"), week_start_equity=Dec("10000"),
                        daily_pnl=Dec("0"), weekly_pnl=Dec("0"), drawdown_pct=Dec("0"),
                        taker_fee_rate=Dec("0.001"))
