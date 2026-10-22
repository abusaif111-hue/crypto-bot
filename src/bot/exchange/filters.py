"""Binance Spot symbol rules, parsed from GET /api/v3/exchangeInfo.

Only the filters this bot's orders can hit are modelled. Unknown filters are
kept in `raw_filters` so nothing is silently dropped.
Reference: https://developers.binance.com/en/docs/products/spot/filters
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot.core.money import D, is_multiple


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal | None = None
    market_min_qty: Decimal | None = None
    market_max_qty: Decimal | None = None
    min_notional: Decimal = Decimal("0")
    max_notional: Decimal | None = None
    notional_applies_to_market: bool = True
    bid_mult_up: Decimal | None = None
    bid_mult_down: Decimal | None = None
    ask_mult_up: Decimal | None = None
    ask_mult_down: Decimal | None = None
    oco_allowed: bool = False
    oto_allowed: bool = False
    opo_allowed: bool = False
    order_types: tuple[str, ...] = ()
    max_asset_qty: Decimal | None = None   # from GET /api/v3/myFilters (account-specific)
    raw_filters: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"


def parse_symbol(info: dict[str, Any], my_filters: dict[str, Any] | None = None) -> SymbolRules:
    """Build SymbolRules from one entry of exchangeInfo['symbols']."""
    f = {flt["filterType"]: flt for flt in info.get("filters", [])}
    if "PRICE_FILTER" not in f or "LOT_SIZE" not in f:
        raise ValueError(f"{info.get('symbol')}: PRICE_FILTER and LOT_SIZE are required")
    pf, lot = f["PRICE_FILTER"], f["LOT_SIZE"]
    mls = f.get("MARKET_LOT_SIZE")
    notional = f.get("NOTIONAL")
    min_notional_f = f.get("MIN_NOTIONAL")
    pps = f.get("PERCENT_PRICE_BY_SIDE")

    min_notional = D("0")
    max_notional = None
    applies_market = True
    if notional:
        min_notional = D(notional.get("minNotional", "0"))
        max_notional = D(notional["maxNotional"]) if notional.get("maxNotional") else None
        applies_market = bool(notional.get("applyMinToMarket", True))
    elif min_notional_f:
        min_notional = D(min_notional_f.get("minNotional", "0"))
        applies_market = bool(min_notional_f.get("applyToMarket", True))

    max_asset = None
    if my_filters:
        for flt in my_filters.get("assetFilters", []):
            if flt.get("filterType") == "MAX_ASSET" and flt.get("asset") == info.get("baseAsset"):
                max_asset = D(flt.get("maxQuantity") or flt.get("limit"))

    def nz(x: Any) -> Decimal | None:
        if x is None:
            return None
        v = D(x)
        return v if v > 0 else None

    return SymbolRules(
        symbol=info["symbol"],
        status=info.get("status", "UNKNOWN"),
        base_asset=info.get("baseAsset", ""),
        quote_asset=info.get("quoteAsset", ""),
        tick_size=D(pf["tickSize"]),
        min_price=D(pf.get("minPrice", "0")),
        max_price=D(pf.get("maxPrice", "0")),
        step_size=D(lot["stepSize"]),
        min_qty=D(lot["minQty"]),
        max_qty=D(lot["maxQty"]),
        market_step_size=nz(mls.get("stepSize")) if mls else None,
        market_min_qty=nz(mls.get("minQty")) if mls else None,
        market_max_qty=nz(mls.get("maxQty")) if mls else None,
        min_notional=min_notional,
        max_notional=max_notional,
        notional_applies_to_market=applies_market,
        bid_mult_up=nz(pps.get("bidMultiplierUp")) if pps else None,
        bid_mult_down=nz(pps.get("bidMultiplierDown")) if pps else None,
        ask_mult_up=nz(pps.get("askMultiplierUp")) if pps else None,
        ask_mult_down=nz(pps.get("askMultiplierDown")) if pps else None,
        oco_allowed=bool(info.get("ocoAllowed", False)),
        oto_allowed=bool(info.get("otoAllowed", False)),
        opo_allowed=bool(info.get("opoAllowed", False)),
        order_types=tuple(info.get("orderTypes", ())),
        max_asset_qty=max_asset,
        raw_filters=tuple(info.get("filters", ())),
    )


def limit_order_violations(
    rules: SymbolRules,
    side: str,
    price: Decimal,
    qty: Decimal,
    reference_price: Decimal | None = None,
    held_base_qty: Decimal = Decimal("0"),
) -> list[str]:
    """Check a LIMIT order against the symbol's filters. Empty list = passes.

    reference_price is the symbol's weighted average or reference price, used by
    PERCENT_PRICE_BY_SIDE; when it is unknown that check is skipped and the caller
    must treat the order as unverified for that filter.
    """
    v: list[str] = []
    price, qty = D(price), D(qty)
    if not rules.is_trading:
        v.append(f"symbol status is {rules.status}")
    if price <= 0 or qty <= 0:
        v.append("price and quantity must be positive")
        return v
    # PRICE_FILTER
    if rules.min_price > 0 and price < rules.min_price:
        v.append("price below minPrice")
    if rules.max_price > 0 and price > rules.max_price:
        v.append("price above maxPrice")
    if rules.tick_size > 0 and not is_multiple(price - rules.min_price if rules.min_price > 0 else price, rules.tick_size):
        v.append("price not a multiple of tickSize")
    # LOT_SIZE
    if qty < rules.min_qty:
        v.append("quantity below minQty")
    if qty > rules.max_qty:
        v.append("quantity above maxQty")
    if not is_multiple(qty, rules.step_size):
        v.append("quantity not a multiple of stepSize")
    # NOTIONAL / MIN_NOTIONAL
    notional = price * qty
    if notional < rules.min_notional:
        v.append(f"notional {notional} below minimum {rules.min_notional}")
    if rules.max_notional is not None and notional > rules.max_notional:
        v.append("notional above maximum")
    # PERCENT_PRICE_BY_SIDE
    if reference_price is not None and reference_price > 0:
        ref = D(reference_price)
        up, down = (rules.bid_mult_up, rules.bid_mult_down) if side == "BUY" else (rules.ask_mult_up, rules.ask_mult_down)
        if up is not None and price > ref * up:
            v.append("price above PERCENT_PRICE_BY_SIDE limit")
        if down is not None and price < ref * down:
            v.append("price below PERCENT_PRICE_BY_SIDE limit")
    # MAX_ASSET (account-specific)
    if side == "BUY" and rules.max_asset_qty is not None and held_base_qty + qty > rules.max_asset_qty:
        v.append("would exceed account MAX_ASSET limit")
    return v
