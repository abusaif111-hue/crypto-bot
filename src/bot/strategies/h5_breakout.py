"""H5: daily breakout with orders placed in advance (docs/h5-plan.md, approved by Sultan on 31 Oct 2026 and
frozen; any change here is a new strategy).

On 4h candles, with daily candles built from them (finished UTC days only), once a day at the first 4h close
after 00:00 UTC, for every coin:
- level: the highest high of the last `entry_days` finished days;
- an order placed in advance: buy once the price trades above the level, limit 0.5% above it, valid until the
  next 00:00 UTC (then a new one replaces it); one order per coin every day (the risk engine still decides,
  and refuses a coin already held);
- stop `stop_atr` x daily ATR(20) below the level; take-profit 3x the stop distance from the level;
- exit at market when a daily close falls below the lowest low of the `exit_days` days before it; time stop
  30 days. No 50-day filter.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from bot.analysis.indicators import atr
from bot.analysis.resample import DAY_MS, daily
from bot.core.money import pct
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, CandleSource, Exit, OpenTrade

LIMIT_ABOVE_PCT = Decimal("0.5")    # fixed: the limit above the level (no chasing beyond it)
ATR_DAYS = 20                       # fixed: daily ATR period
TAKE_PROFIT_R = Decimal("3")        # fixed: as every strategy
HORIZON_DAYS = 30                   # fixed: time stop


@dataclass(frozen=True)
class AdvanceOrder:
    """Today's planned order for one coin: buy above `trigger`, at most `limit`, with its stop and target."""
    trigger: Decimal
    limit: Decimal
    stop: Decimal
    target: Decimal
    valid_until: float


@dataclass(frozen=True)
class DailyBreakout:
    symbols: list[str]
    entry_days: int = 20                    # tunable: the breakout level's lookback (days)
    stop_atr: Decimal = Decimal("2.0")      # tunable: stop distance in daily ATR(20)s, below the level
    exit_days: int = 10                     # tunable: exit on a close below this many days' low
    name: str = "H5"
    interval: str = "4h"

    def params(self) -> dict[str, object]:
        return {"entry_days": self.entry_days, "stop_atr": self.stop_atr, "exit_days": self.exit_days}

    def with_params(self, **p: object) -> "DailyBreakout":
        return replace(self, **p)

    def history_needed(self) -> int:
        days = max(self.entry_days, self.exit_days + 1, ATR_DAYS * 3) + 3     # + the running day and 2 spare
        return days * 6

    @staticmethod
    def new_day(now: float) -> bool:
        """True at the first 4h close after midnight UTC (a daily candle has just finished)."""
        return int(now * 1000) % DAY_MS < INTERVAL_MS["4h"]

    def order(self, days: list[Candle], now: float) -> AdvanceOrder | None:
        """Today's order from finished daily candles (oldest first); None without enough history."""
        if len(days) < max(self.entry_days, ATR_DAYS * 3):
            return None
        a = atr(days[-ATR_DAYS * 3:], ATR_DAYS)[-1]
        if a is None or a <= 0:
            return None
        level = max(d.high for d in days[-self.entry_days:])
        stop = level - self.stop_atr * a
        if stop <= 0:
            return None
        midnight = (int(now * 1000) // DAY_MS + 1) * DAY_MS / 1000
        return AdvanceOrder(level, level * (1 + pct(LIMIT_ABOVE_PCT)), stop, level + TAKE_PROFIT_R * (level - stop),
                            midnight)

    def intent(self, symbol: str, o: AdvanceOrder) -> TradeIntent:
        return TradeIntent(symbol=symbol, side="BUY", entry_price=o.limit, stop_price=o.stop,
                           take_profit_price=o.target,
                           strategy=self.name, horizon_hours=HORIZON_DAYS * 24,
                           invalidation=f"a daily close below the {self.exit_days}-day low",
                           trigger_price=o.trigger, valid_until=o.valid_until)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        if not self.new_day(now):
            return []
        out = []
        for s in self.symbols:
            o = self.order(daily(data.closed_candles(s, self.interval, limit=self.history_needed())), now)
            if o is not None:
                out.append(self.intent(s, o))
        return out

    # ------------------------------------------------------------ exits and the random baseline
    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        days = daily(candles)
        a = atr(days[-ATR_DAYS * 3:], ATR_DAYS)[-1] if len(days) > ATR_DAYS else None
        return None if a is None or a <= 0 else (entry - self.stop_atr * a, HORIZON_DAYS * 24)

    def take_profit(self, entry: Decimal, stop: Decimal) -> Decimal:
        return entry + TAKE_PROFIT_R * (entry - stop)

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        """Once a day: sell at market when the daily close is below the lowest low of the exit_days before it."""
        if not self.new_day(now):
            return []
        out: list[Action] = []
        for t in open_trades:
            days = daily(data.closed_candles(t.symbol, self.interval, limit=self.history_needed()))
            if len(days) < self.exit_days + 1:
                continue
            low = min(d.low for d in days[-1 - self.exit_days:-1])
            if days[-1].close < low:
                out.append(Exit(t.symbol, f"the daily close {days[-1].close} fell below the {self.exit_days}-day "
                                          f"low {low}"))
        return out
