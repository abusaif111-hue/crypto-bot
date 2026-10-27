"""Daily trend-pullback signals (D1, `bot.strategies.daily_pullback`): LONG or NO TRADE per coin, simulated only.

For each coin it shows D1's verdict and, for a LONG, what the risk engine would do with it: the size that
puts 0.25% of the account at risk, the reward-to-risk after the engine's own slippage and fees, and every
limit (daily and weekly loss, positions, spread, volume), all with the settings unchanged. A LONG the engine
rejects is shown as NO TRADE with the engine's reason. The account is hypothetical: the equity and the
loss already taken today are given by the operator.

It places no order and touches no account: it reads public market data only and is not wired into any
runner. Each run is appended to its own log (`signals.jsonl` in the log folder), never to a runner's folder.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from bot.core.config import AppConfig
from bot.core.money import ZERO, D, fmt, plain
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, Decision, MarketSnapshot, Mode
from bot.strategies.daily_pullback import TAKE_PROFIT_R, DailyPullback, Verdict

LOG_FILE = "signals.jsonl"
MAJORS = ("BTCUSDT", "ETHUSDT")          # one correlation group (R-08), as everywhere in the bot


class MarketData(Protocol):
    def closed_candles(self, symbol: str, interval: str, start_ms: int | None = None,
                       end_ms: int | None = None, limit: int = 500) -> list[Candle]: ...

    def rules(self, symbol: str) -> SymbolRules: ...

    def snapshot(self, symbol: str) -> MarketSnapshot: ...


@dataclass(frozen=True)
class CoinSignal:
    symbol: str
    long: bool                       # True only when D1 says LONG and the risk engine accepts it
    reason: str
    verdict: Verdict | None = None
    decision: Decision | None = None


def corr_group(symbol: str) -> str:
    return "majors" if symbol in MAJORS else symbol


def account(equity: Decimal, loss_today: Decimal, fee: Decimal) -> AccountState:
    """A hypothetical account with no open positions, `loss_today` USDT already lost today (and this week)."""
    return AccountState(mode=Mode.RUNNING, equity=equity - loss_today, free_quote=equity - loss_today,
                        day_start_equity=equity, week_start_equity=equity, daily_pnl=-loss_today,
                        weekly_pnl=-loss_today, drawdown_pct=D(0), taker_fee_rate=fee)


def coin_signal(strategy: DailyPullback, data: MarketData, symbol: str, cfg: AppConfig, state: AccountState,
                now: float) -> CoinSignal:
    try:
        cs = data.closed_candles(symbol, strategy.interval, limit=strategy.history_needed())
    except Exception as e:                                   # market data down: no trade, say why
        return CoinSignal(symbol, False, f"market data unavailable ({type(e).__name__}: {e})")
    v = strategy.signal(cs, now)
    intent = strategy.intent(symbol, v)
    if intent is None:
        return CoinSignal(symbol, False, v.reason, v)
    try:
        rules, snap = data.rules(symbol), data.snapshot(symbol)
    except Exception as e:
        return CoinSignal(symbol, False, f"could not read the market for the risk check ({type(e).__name__}: {e})", v)
    d = evaluate(intent, state, snap, rules, cfg, corr_group(symbol))
    if not d.accepted:
        return CoinSignal(symbol, False, f"D1 would buy, but the risk engine says no ({d.code.value}): {d.reason}",
                          v, d)
    return CoinSignal(symbol, True, v.reason, v, d)


def report(strategy: DailyPullback, data: MarketData, cfg: AppConfig, equity: Decimal, now: float,
           loss_today: Decimal = ZERO, fee: Decimal = Decimal("0.001")) -> list[CoinSignal]:
    state = account(D(equity), D(loss_today), D(fee))
    return [coin_signal(strategy, data, s, cfg, state, now) for s in strategy.symbols]


def render(signals: list[CoinSignal], equity: Decimal, loss_today: Decimal, now: float) -> str:
    L = [f"Daily trend-pullback signals (D1, Spot, long-only), {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}",
         f"Hypothetical account: {fmt(equity)} USDT, {fmt(loss_today)} USDT lost today. SIMULATED: nothing is sent "
         f"to Binance.", ""]
    for c in signals:
        if not c.long:
            L += [f"{c.symbol}: NO TRADE", f"  Reason: {c.reason}", ""]
            continue
        d = c.decision.details
        entry, stop, tp = D(d["entry"]), D(d["stop"]), D(d["take_profit"])
        risk = c.decision.planned_risk
        L += [f"{c.symbol}: LONG",
              f"  Entry (limit) .. {plain(entry)}",
              f"  Stop-loss ...... {plain(stop)} ({fmt((entry - stop) / entry * 100)}% below)",
              f"  Take-profit .... {plain(tp)} ({fmt(TAKE_PROFIT_R)}x the stop distance)",
              f"  Size ........... {plain(c.decision.qty)} ({fmt(c.decision.qty * entry)} USDT)",
              f"  Risk ........... {fmt(risk)} USDT ({risk / equity * 100:.2f}% of equity) if the stop fills with "
              f"the engine's slippage",
              f"  Reward/risk .... {fmt(TAKE_PROFIT_R)} before costs, {c.decision.reward_risk:.2f} after the "
              f"engine's slippage and fees",
              f"  Reason ......... {c.reason}",
              "  Exits .......... stop, take-profit, 72-hour time stop, or a sale when the daily close falls "
              "below its EMA", ""]
    L += ["Daily and weekly loss limits, the drawdown breaker, position and correlation limits are the risk engine's",
          "own, unchanged. D1 is untested: no backtest, paper or live result exists for it."]
    return "\n".join(L) + "\n"


def log_row(c: CoinSignal, now: float, equity: Decimal) -> dict[str, Any]:
    row: dict[str, Any] = {"at": now, "symbol": c.symbol, "signal": "LONG" if c.long else "NO TRADE",
                           "reason": c.reason, "equity": str(equity)}
    v, d = c.verdict, c.decision
    if v is not None and v.candle_ms is not None:
        row["candle_open_ms"] = v.candle_ms
    if d is not None:
        row["engine"] = "accepted" if d.accepted else (d.code.value if d.code else "rejected")
    if c.long and d is not None:
        row.update(entry=d.details["entry"], stop=d.details["stop"], take_profit=d.details["take_profit"],
                   qty=str(d.qty), planned_risk=str(d.planned_risk), reward_risk=str(d.reward_risk))
    return row


def record(log_dir: Path, signals: list[CoinSignal], now: float, equity: Decimal) -> Path:
    """Append one line per coin to the signal log (its own folder)."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = log_dir / LOG_FILE
    with open(path, "a") as f:
        for c in signals:
            f.write(json.dumps(log_row(c, now, equity)) + "\n")
    return path
