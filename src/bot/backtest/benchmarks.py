"""Benchmarks to compare strategies against (design package, "Baselines").

These are yardsticks, not bot strategies: buy-and-hold puts everything in at
once, which the risk rules would never allow (50% invested cap), and no order
goes through the risk engine. They pay the same fee, half-spread and entry
slippage, and are valued at what selling would bring (bid, slippage, fee).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bot.backtest.engine import EquityPoint
from bot.backtest.replay import CostModel
from bot.core.config import AppConfig
from bot.core.money import ZERO, pct
from bot.exchange.interface import Candle


def _grid(candles: dict[str, list[Candle]], start_ms: int, end_ms: int | None) -> list[int]:
    """Open times where every coin has a candle."""
    sets = [{c.open_time for c in cs if c.open_time >= start_ms and (end_ms is None or c.close_time < end_ms)}
            for cs in candles.values()]
    return sorted(set.intersection(*sets))


def cash(candles: dict[str, list[Candle]], start_ms: int, end_ms: int | None, start: Decimal) -> list[EquityPoint]:
    any_coin = next(iter(candles.values()))
    step = any_coin[0].close_time - any_coin[0].open_time + 1
    return [EquityPoint(t + step, start, False) for t in _grid(candles, start_ms, end_ms)]


def _costs(cost: CostModel, cfg: AppConfig) -> tuple[Decimal, Decimal, Decimal]:
    slip = cost.entry_slippage_pct if cost.entry_slippage_pct is not None else cfg.risk.entry_slippage_pct
    return pct(cost.half_spread_pct), pct(slip * cost.slippage_multiplier), cost.taker_fee


def rebalanced(candles: dict[str, list[Candle]], weights: dict[str, Decimal], cfg: AppConfig, cost: CostModel,
               start_ms: int, end_ms: int | None, start: Decimal, monthly: bool) -> list[EquityPoint]:
    """Hold coins at fixed weights; rebalance at the first candle of each month if `monthly`."""
    hs, slip, fee = _costs(cost, cfg)
    by_time = {s: {c.open_time: c for c in candles[s]} for s in weights}
    grid = _grid({s: candles[s] for s in weights}, start_ms, end_ms)
    qty = {s: ZERO for s in weights}
    usdt = start
    month = None
    out = []
    for t in grid:
        m = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")
        if month is None or (monthly and m != month):
            opens = {s: by_time[s][t].open for s in weights}
            value = usdt + sum((qty[s] * opens[s] * (1 - hs) * (1 - slip) * (1 - fee) for s in weights), ZERO)
            for s, w in weights.items():                     # sell what is over target first
                target = value * w / (opens[s] * (1 - hs) * (1 - slip) * (1 - fee))
                if qty[s] > target:
                    usdt += (qty[s] - target) * opens[s] * (1 - hs) * (1 - slip) * (1 - fee)
                    qty[s] = target
            for s, w in weights.items():                     # then buy what is under
                buy_px = opens[s] * (1 + hs) * (1 + slip) * (1 + fee)
                target = value * w / buy_px
                if qty[s] < target:
                    spend = min(usdt, (target - qty[s]) * buy_px)
                    qty[s] += spend / buy_px
                    usdt -= spend
            month = m
        closes = {s: by_time[s][t].close for s in weights}
        c0 = by_time[next(iter(weights))][t]
        liquid = usdt + sum((qty[s] * closes[s] * (1 - hs) * (1 - slip) * (1 - fee) for s in weights), ZERO)
        out.append(EquityPoint(c0.close_time + 1, liquid, True))
    return out


def buy_and_hold(candles, symbol, cfg, cost, start_ms, end_ms, start) -> list[EquityPoint]:
    return rebalanced({symbol: candles[symbol]}, {symbol: Decimal(1)}, cfg, cost, start_ms, end_ms, start,
                      monthly=False)


def fifty_fifty_monthly(candles, cfg, cost, start_ms, end_ms, start) -> list[EquityPoint]:
    return rebalanced(candles, {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")}, cfg, cost, start_ms,
                      end_ms, start, monthly=True)
