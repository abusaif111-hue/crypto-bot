"""The risk engine: the only gate between a trade idea and an order.

`evaluate()` is a pure function. It reads the intent, the account state, the
market snapshot and the symbol's exchange rules, and returns either an accepted
quantity or a rejection with a code and a plain-language reason. It does no I/O,
so the same code runs in backtests, paper trading and live.

Rule IDs (R-01 ...) refer to section 4 of the Stage 1 design package.
"""
from __future__ import annotations

from decimal import Decimal

from bot.core.config import AppConfig
from bot.core.money import ZERO, D, floor_price, floor_to_step, pct
from bot.exchange.filters import SymbolRules, limit_order_violations
from bot.risk.models import (
    AccountState,
    Decision,
    MarketSnapshot,
    Mode,
    OpenPosition,
    RejectCode,
    TradeIntent,
)


# ---------------------------------------------------------------- exposure maths
def position_open_risk(p: OpenPosition, stop_slippage: Decimal, fee: Decimal) -> Decimal:
    """USDT still at risk if the stop fills with slippage, measured from the current bid."""
    stop_fill = p.stop_price * (1 - stop_slippage)
    loss_per_unit = p.current_bid - stop_fill
    exit_fee = fee * stop_fill * p.qty
    return max(ZERO, loss_per_unit * p.qty) + exit_fee


def total_open_risk(state: AccountState, cfg: AppConfig) -> Decimal:
    return sum(
        (position_open_risk(p, pct(cfg.risk.stop_slippage_for(p.symbol)), state.taker_fee_rate) for p in state.positions),
        ZERO,
    )


def invested_value(state: AccountState) -> Decimal:
    return sum((p.qty * p.current_bid for p in state.positions), ZERO)


def breached_limits(state: AccountState, cfg: AppConfig) -> list[RejectCode]:
    """Loss and drawdown limits that are already breached (R-02, R-03, R-09)."""
    r = cfg.risk
    out = []
    if state.day_start_equity > 0 and -state.daily_pnl >= pct(r.daily_loss_pct) * state.day_start_equity:
        out.append(RejectCode.DAILY_LOSS)
    if state.week_start_equity > 0 and -state.weekly_pnl >= pct(r.weekly_loss_pct) * state.week_start_equity:
        out.append(RejectCode.WEEKLY_LOSS)
    if state.drawdown_pct >= r.drawdown_breaker_pct:
        out.append(RejectCode.DRAWDOWN)
    return out


# ---------------------------------------------------------------- main gate
def evaluate(
    intent: TradeIntent,
    state: AccountState,
    market: MarketSnapshot,
    rules: SymbolRules,
    cfg: AppConfig,
    corr_group: str,
) -> Decision:
    r = cfg.risk
    fee = D(state.taker_fee_rate)

    # 1. Operating mode
    if state.mode is not Mode.RUNNING:
        return Decision.reject(RejectCode.MODE, f"new entries are not allowed in mode {state.mode.value}")

    # 2. Scope: long-only Spot, approved universe, tradeable symbol
    if intent.side != "BUY":
        return Decision.reject(RejectCode.SIDE, "only BUY entries are allowed (Spot, no shorting)")
    if intent.symbol not in cfg.universe or rules.symbol != intent.symbol:
        return Decision.reject(RejectCode.UNIVERSE, f"{intent.symbol} is not in the approved universe")
    if not rules.is_trading:
        return Decision.reject(RejectCode.SYMBOL, f"{intent.symbol} status is {rules.status}")
    if not rules.oco_allowed:
        return Decision.reject(RejectCode.SYMBOL, f"{intent.symbol} does not allow OCO, so exits cannot be protected")

    # 3. Sanity of inputs
    for name, value in (("equity", state.equity), ("entry", intent.entry_price), ("stop", intent.stop_price),
                        ("take profit", intent.take_profit_price), ("bid", market.best_bid), ("ask", market.best_ask)):
        if D(value) <= 0:
            return Decision.reject(RejectCode.BAD_INPUT, f"{name} must be positive")
    if market.best_ask < market.best_bid:
        return Decision.reject(RejectCode.BAD_INPUT, "crossed book: ask below bid")

    # 4. Exit plan must be complete and coherent
    entry = floor_price(intent.entry_price, rules.tick_size)
    stop = floor_price(intent.stop_price, rules.tick_size)
    tp = floor_price(intent.take_profit_price, rules.tick_size)
    if not (stop < entry < tp):
        return Decision.reject(RejectCode.EXIT_PLAN, "exit plan needs stop < entry < take-profit")
    if intent.horizon_hours <= 0 or not intent.invalidation.strip():
        return Decision.reject(RejectCode.EXIT_PLAN, "exit plan needs a holding horizon and an invalidation condition")
    if intent.trigger_price is not None:                     # an order placed in advance (H5)
        trigger = floor_price(intent.trigger_price, rules.tick_size)
        if not (stop < trigger <= entry) or intent.valid_until is None:
            return Decision.reject(RejectCode.EXIT_PLAN,
                                   "an order placed in advance needs stop < trigger <= its limit, and an expiry")

    # 5. Data quality (R-12, R-13)
    if market.data_age_s > r.max_data_age_s:
        return Decision.reject(RejectCode.STALE_DATA, f"price data is {market.data_age_s}s old (max {r.max_data_age_s}s)")
    if abs(market.clock_skew_ms) > r.max_clock_skew_ms:
        return Decision.reject(RejectCode.CLOCK, f"clock skew {market.clock_skew_ms} ms (max {r.max_clock_skew_ms} ms)")

    # 6. Market quality (R-11)
    mid = (market.best_bid + market.best_ask) / 2
    spread_pct = (market.best_ask - market.best_bid) / mid * 100
    if spread_pct > r.max_spread_pct:
        return Decision.reject(RejectCode.MARKET_QUALITY, f"spread {spread_pct:.3f}% above {r.max_spread_pct}%")
    if market.quote_volume_24h < r.min_quote_volume_24h:
        return Decision.reject(RejectCode.MARKET_QUALITY, "24h volume below minimum")

    # 7. Portfolio rules (R-14, R-04, R-08)
    if any(p.symbol == intent.symbol for p in state.positions):
        return Decision.reject(RejectCode.EXISTING_POSITION, "already holding this coin; adding to a position is not allowed")
    if len(state.positions) >= r.max_open_positions:
        return Decision.reject(RejectCode.MAX_POSITIONS, f"already {len(state.positions)} open positions")
    if sum(1 for p in state.positions if p.corr_group == corr_group) >= r.max_per_corr_group:
        return Decision.reject(RejectCode.CORRELATION, f"correlation group '{corr_group}' is full")

    # 8. Loss limits and drawdown breaker (R-02, R-03, R-09)
    breached = breached_limits(state, cfg)
    if breached:
        return Decision.reject(breached[0], "a loss or drawdown limit is already reached", breached=[b.value for b in breached])

    # 9. Per-unit economics with slippage and fees
    s_in = pct(r.entry_slippage_pct)
    s_stop = pct(r.stop_slippage_for(intent.symbol))
    pe = entry * (1 + s_in)                     # P_e'
    ps = stop * (1 - s_stop)                    # P_s'
    loss_per_unit = (pe - ps) + fee * (pe + ps)  # ℓ
    reward_per_unit = (tp - pe) - fee * (pe + tp)
    if loss_per_unit <= 0:
        return Decision.reject(RejectCode.BAD_INPUT, "loss per unit is not positive")
    rr = reward_per_unit / loss_per_unit
    if rr < r.min_reward_risk:
        return Decision.reject(RejectCode.REWARD_RISK, f"reward-to-risk {rr:.2f} after costs is below {r.min_reward_risk}", reward_risk=str(rr))

    # 10. Risk budget B = the smallest remaining budget (stricter limit wins)
    E = state.equity
    open_risk = total_open_risk(state, cfg)
    daily_loss = max(ZERO, -state.daily_pnl)
    weekly_loss = max(ZERO, -state.weekly_pnl)
    budgets = {
        "per_trade": pct(r.risk_per_trade_pct) * E,                                         # R-01
        "daily": pct(r.daily_loss_pct) * state.day_start_equity - daily_loss - open_risk,  # R-02
        "weekly": pct(r.weekly_loss_pct) * state.week_start_equity - weekly_loss - open_risk,  # R-03
        "open_risk": pct(r.max_open_risk_pct) * E - open_risk,                              # R-05
    }
    binding = min(budgets, key=lambda k: budgets[k])
    B = budgets[binding]
    if B <= 0:
        return Decision.reject(RejectCode.RISK_BUDGET, f"no risk budget left ({binding} limit)", budgets={k: str(v) for k, v in budgets.items()})

    # 11. Size: smallest of every cap, rounded DOWN to the step size
    invested = invested_value(state)
    caps = {
        "risk": B / loss_per_unit,
        "invested": (pct(r.max_invested_pct) * E - invested) / pe,        # R-06
        "coin": pct(r.max_coin_exposure_pct) * E / pe,                    # R-07
        "cash": state.free_quote / (pe * (1 + fee)),
        "depth": market.ask_depth_quote / (r.min_depth_multiple * pe),    # R-11 depth
        "lot_max": rules.max_qty,
    }
    size_by = min(caps, key=lambda k: caps[k])
    qty = floor_to_step(max(ZERO, caps[size_by]), rules.step_size)

    if qty <= 0:
        return Decision.reject(RejectCode.EXCHANGE_MIN, f"size rounds to zero (limited by {size_by})")
    violations = limit_order_violations(rules, "BUY", entry, qty, market.reference_price)
    if violations:
        return Decision.reject(RejectCode.EXCHANGE_MIN, "order fails exchange rules: " + "; ".join(violations),
                               qty=str(qty), limited_by=size_by)
    # The exits must also be valid orders for the same quantity
    exit_violations = limit_order_violations(rules, "SELL", stop, qty) + limit_order_violations(rules, "SELL", tp, qty)
    if exit_violations:
        return Decision.reject(RejectCode.EXCHANGE_MIN, "protective exits would fail exchange rules: " + "; ".join(exit_violations))

    planned_risk = qty * loss_per_unit

    # 12. Defence in depth: re-check every limit on the final numbers
    problems = _verify(qty, pe, planned_risk, state, cfg, open_risk, invested)
    if problems:
        return Decision.reject(RejectCode.BAD_INPUT, "internal check failed: " + "; ".join(problems))

    return Decision(
        accepted=True,
        qty=qty,
        planned_risk=planned_risk,
        reward_risk=rr,
        reason=(f"BUY {qty} {intent.symbol} at {entry}: stop {stop}, target {tp}; "
                f"worst planned loss {planned_risk:.2f} USDT ({planned_risk / E * 100:.3f}% of equity), "
                f"reward-to-risk {rr:.2f}; size limited by {size_by}, risk budget limited by {binding}"),
        details={
            "entry": str(entry), "stop": str(stop), "take_profit": str(tp),
            "loss_per_unit": str(loss_per_unit), "budget": str(B), "budget_limited_by": binding,
            "size_limited_by": size_by, "open_risk_before": str(open_risk),
            "notional": str(qty * entry),
        },
    )


def _verify(qty: Decimal, pe: Decimal, planned_risk: Decimal, state: AccountState, cfg: AppConfig,
            open_risk: Decimal, invested: Decimal) -> list[str]:
    r = cfg.risk
    E = state.equity
    p: list[str] = []
    if planned_risk > pct(r.risk_per_trade_pct) * E:
        p.append("per-trade risk")
    if open_risk + planned_risk > pct(r.max_open_risk_pct) * E:
        p.append("total open risk")
    if max(ZERO, -state.daily_pnl) + open_risk + planned_risk > pct(r.daily_loss_pct) * state.day_start_equity:
        p.append("daily loss budget")
    if max(ZERO, -state.weekly_pnl) + open_risk + planned_risk > pct(r.weekly_loss_pct) * state.week_start_equity:
        p.append("weekly loss budget")
    if invested + qty * pe > pct(r.max_invested_pct) * E:
        p.append("invested capital")
    if qty * pe > pct(r.max_coin_exposure_pct) * E:
        p.append("coin exposure")
    if len(state.positions) + 1 > r.max_open_positions:
        p.append("open positions")
    return p
