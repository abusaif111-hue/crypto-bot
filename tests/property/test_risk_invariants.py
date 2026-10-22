"""T-RISK-010..020: whatever the account state, an accepted trade never breaks a limit.

Hypothesis generates thousands of random accounts, positions, prices and configs
(within the hard ceilings) and checks every limit on every accepted decision.
"""
from decimal import Decimal as Dec

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bot.core.config import AppConfig, RiskConfig
from bot.core.money import is_multiple, pct
from bot.exchange.filters import parse_symbol
from bot.risk.engine import evaluate, invested_value, total_open_risk
from bot.risk.models import AccountState, MarketSnapshot, Mode, OpenPosition, TradeIntent
from tests.conftest import exchange_info_symbol

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
RULES = {s: parse_symbol(exchange_info_symbol(s, tick="0.01", step="0.00001")) for s in SYMBOLS}


def dec(lo, hi, places=2):
    return st.decimals(min_value=Dec(str(lo)), max_value=Dec(str(hi)), places=places, allow_nan=False, allow_infinity=False)


@st.composite
def scenario(draw, healthy=False):
    risk = RiskConfig(
        risk_per_trade_pct=draw(dec("0.05", "0.5")),
        daily_loss_pct=draw(dec("0.5", "1.5")),
        weekly_loss_pct=draw(dec("1", "3")),
        max_open_positions=3 if healthy else draw(st.sampled_from([1, 2, 3, 3, 3])),
        max_open_risk_pct=draw(dec("0.5", "1.5")),
        max_invested_pct=draw(dec("10", "50")),
        max_coin_exposure_pct=draw(dec("5", "25")),
        max_per_corr_group=2 if healthy else draw(st.sampled_from([1, 2, 2])),
        min_reward_risk=Dec("1") if healthy else draw(dec("1", "2")),
    )
    cfg = AppConfig(universe=SYMBOLS, risk=risk)
    equity = draw(dec("2000" if healthy else "100", "1000000"))
    day_start = equity * draw(dec("0.95", "1.05", 3))
    week_start = equity * draw(dec("0.95", "1.05", 3))
    n_pos = draw(st.sampled_from([0, 1, 1, 2] if healthy else [0, 0, 1, 1, 2, 3]))
    syms = draw(st.lists(st.sampled_from(SYMBOLS[1:]), min_size=n_pos, max_size=n_pos, unique=True))
    positions = []
    for s in syms:
        bid = draw(dec("1", "5000"))
        qty = (equity * draw(dec("0.001", "0.15", 3)) / bid).quantize(Dec("0.00001"))
        positions.append(OpenPosition(symbol=s, qty=qty, avg_entry=bid, stop_price=bid * draw(dec("0.85", "0.99", 3)),
                                      current_bid=bid, corr_group=draw(st.sampled_from(["alts"] if healthy else ["majors", "alts"]))))
    invested = sum((p.qty * p.current_bid for p in positions), Dec(0))
    state = AccountState(
        mode=Mode.RUNNING, equity=equity, free_quote=max(Dec(0), equity - invested) * draw(dec("0", "1", 3)),
        day_start_equity=day_start, week_start_equity=week_start,
        daily_pnl=-equity * draw(dec("0", "0.003" if healthy else "0.016", 4)), weekly_pnl=-equity * draw(dec("0", "0.006" if healthy else "0.032", 4)),
        drawdown_pct=draw(dec("0", "3" if healthy else "6.5")), positions=tuple(positions), taker_fee_rate=draw(dec("0", "0.002", 5)),
    )
    entry = draw(dec("10", "100000"))
    intent = TradeIntent(symbol="BTCUSDT", side="BUY", entry_price=entry,
                         stop_price=entry * draw(dec("0.90" if healthy else "0.80", "0.985" if healthy else "0.995", 3)),
                         take_profit_price=entry * draw(dec("1.05" if healthy else "1.005", "1.5", 3)),
                         strategy="prop", horizon_hours=24, invalidation="test")
    market = MarketSnapshot(best_bid=entry * Dec("0.9999"), best_ask=entry, quote_volume_24h=Dec("1e9"),
                            ask_depth_quote=draw(dec("1000", "100000000")), data_age_s=Dec("1"), clock_skew_ms=0,
                            reference_price=entry)
    return cfg, state, intent, market, draw(st.sampled_from(["majors", "alts"]))


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.one_of(scenario(), scenario(healthy=True), scenario(healthy=True)))
def test_accepted_trades_never_exceed_any_limit(sc):
    check_limits(sc)


def check_limits(sc):
    cfg, state, intent, market, group = sc
    d = evaluate(intent, state, market, RULES["BTCUSDT"], cfg, group)
    if not d.accepted:
        assert d.code is not None and d.reason
        return
    r, E = cfg.risk, state.equity
    open_risk = total_open_risk(state, cfg)
    entry_eff = Dec(d.details["entry"]) * (1 + pct(r.entry_slippage_pct))
    rules = RULES["BTCUSDT"]
    assert d.qty > 0 and is_multiple(d.qty, rules.step_size)
    assert d.qty * Dec(d.details["entry"]) >= rules.min_notional
    assert d.planned_risk <= pct(r.risk_per_trade_pct) * E                                       # R-01
    assert max(Dec(0), -state.daily_pnl) + open_risk + d.planned_risk <= pct(r.daily_loss_pct) * state.day_start_equity    # R-02
    assert max(Dec(0), -state.weekly_pnl) + open_risk + d.planned_risk <= pct(r.weekly_loss_pct) * state.week_start_equity  # R-03
    assert len(state.positions) + 1 <= r.max_open_positions                                       # R-04
    assert open_risk + d.planned_risk <= pct(r.max_open_risk_pct) * E                             # R-05
    assert invested_value(state) + d.qty * entry_eff <= pct(r.max_invested_pct) * E               # R-06
    assert d.qty * entry_eff <= pct(r.max_coin_exposure_pct) * E                                  # R-07
    assert sum(1 for p in state.positions if p.corr_group == group) < r.max_per_corr_group         # R-08
    assert state.drawdown_pct < r.drawdown_breaker_pct                                            # R-09
    assert d.reward_risk >= r.min_reward_risk                                                     # R-10
    assert d.qty * entry_eff * (1 + state.taker_fee_rate) <= state.free_quote                     # cash


@settings(max_examples=500, deadline=None)
@given(scenario(), st.sampled_from([Mode.ENTRIES_BLOCKED, Mode.PAUSED, Mode.EMERGENCY_FLATTEN]))
def test_nothing_is_accepted_outside_running_mode(sc, mode):
    cfg, state, intent, market, group = sc
    from dataclasses import replace
    assert not evaluate(intent, replace(state, mode=mode), market, RULES["BTCUSDT"], cfg, group).accepted
