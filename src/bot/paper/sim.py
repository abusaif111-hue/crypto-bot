"""Simulated Binance Spot exchange for paper trading (module 3).

`SimExchange` offers the same read interface as the real adapter
(`SpotReadVenue`): market data comes from the venue it wraps, normally the
read-only Binance adapter, while balances and fees are simulated. The account
starts with 10,000 USDT of simulated money.

The only way to open a position is `submit(intent)`, which builds the account
state and market snapshot and calls `bot.risk.engine.evaluate()`. Nothing here
accepts a ready-made `Decision`, so no entry can skip the risk engine.

Fill model (see `bot.paper.fills`):
- Entry: a limit order that fills immediately in full or not at all (IOC),
  at ask x (1 + entry slippage). If that is above the worst entry the risk
  engine sized for, or the ask is already at or below the stop, nothing fills.
- Exits: a simulated stop-loss / take-profit pair (one cancels the other) for
  the full quantity, placed as soon as the entry fills. Stops fill at
  min(stop, price) x (1 - stop slippage); take-profit fills at its limit.
  A time stop sells at market when the intent's holding horizon runs out.
- Every fill pays the taker fee (0.1%), charged in USDT. Real Binance takes
  a buy's fee from the coin received unless fees are paid in BNB: module 5
  must handle that (verify on testnet).

State is in memory only; a restart loses it.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from bot.core.config import AppConfig
from bot.core.ids import client_order_id, new_intent_id
from bot.core.money import ZERO, D, fmt, pct
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Book, Candle, SpotReadVenue
from bot.market.data import MarketDataService, MarketDataUnavailable
from bot.paper import fills
from bot.portfolio.accounting import Balance, EquityIndex, MissingPrice, PeriodTracker, value_equity
from bot.risk.engine import evaluate
from bot.risk.models import (
    AccountState, Decision, MarketSnapshot, Mode, OpenPosition, RejectCode, TradeIntent,
)
from bot.risk.modes import ModeController

STARTING_QUOTE = Decimal("10000")
BINANCE_TAKER_FEE = Decimal("0.001")     # 0.1%, Binance Spot standard taker rate
QUOTE = "USDT"

# BTC and ETH correlate at about 0.85, so they share a group. A coin not listed
# here is also put in "majors": most altcoins follow BTC, and the cautious guess wins.
CORR_GROUPS = {"BTCUSDT": "majors", "ETHUSDT": "majors"}
DEFAULT_CORR_GROUP = "majors"


def corr_group(symbol: str) -> str:
    return CORR_GROUPS.get(symbol, DEFAULT_CORR_GROUP)


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    symbol: str
    side: str            # BUY or SELL
    kind: str            # ENTRY, STOP_LOSS, TAKE_PROFIT or TIME_STOP
    qty: Decimal
    price: Decimal
    fee: Decimal         # USDT
    at: float            # unix seconds
    slippage: Decimal = Decimal("0")   # USDT lost to slippage on this fill (model)


@dataclass(frozen=True)
class SimPosition:
    intent_id: uuid.UUID
    symbol: str
    base_asset: str
    strategy: str
    qty: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    planned_risk: Decimal
    opened_at: float
    expires_at: float
    corr_group: str

    @property
    def exit_list_id(self) -> str:
        return client_order_id(self.intent_id, "list")


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    strategy: str
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    entry_fee: Decimal
    exit_fee: Decimal
    pnl: Decimal          # net of both fees
    planned_risk: Decimal
    gapped: bool          # the stop filled below its slippage allowance
    opened_at: float
    closed_at: float


@dataclass(frozen=True)
class EntryResult:
    decision: Decision
    filled: bool
    fill: Fill | None = None
    reason: str = ""


def as_utc(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


@dataclass
class SimExchange:
    cfg: AppConfig
    market: SpotReadVenue
    clock: Callable[[], float] = time.time
    starting_quote: Decimal = STARTING_QUOTE
    fee_rate: Decimal = BINANCE_TAKER_FEE
    entry_slippage_pct: Decimal | None = None   # None: the risk config's entry allowance
    exit_slippage_pct: Decimal | None = None    # None: the risk config's stop allowance for the symbol
    slippage_multiplier: Decimal = Decimal("1")  # backtests double costs with 2

    quote: Decimal = field(init=False)
    created_at: float = field(init=False)
    positions: dict[str, SimPosition] = field(init=False, default_factory=dict)
    fills: list[Fill] = field(init=False, default_factory=list)
    trades: list[ClosedTrade] = field(init=False, default_factory=list)
    data: MarketDataService = field(init=False)
    modes: ModeController = field(init=False)
    index: EquityIndex = field(init=False, default_factory=EquityIndex)
    periods: PeriodTracker = field(init=False, default_factory=PeriodTracker)
    _bids: dict[str, Decimal] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if D(self.starting_quote) <= 0:
            raise ValueError("starting balance must be positive")
        if D(self.fee_rate) < 0:
            raise ValueError("fee rate must not be negative")
        if D(self.slippage_multiplier) <= 0:
            raise ValueError("slippage_multiplier must be positive")
        for name in ("entry_slippage_pct", "exit_slippage_pct"):
            v = getattr(self, name)
            if v is not None and D(v) < 0:
                raise ValueError(f"{name} must not be negative")
        self.quote = D(self.starting_quote)
        self.created_at = self.clock()
        self.fee_rate = D(self.fee_rate)
        self.data = MarketDataService(self.market, self.cfg, self.clock)
        self.modes = ModeController(self.cfg)          # starts ENTRIES_BLOCKED, like the real bot
        self.index.start(self.quote)
        self.periods.update(as_utc(self.clock()), self.quote)

    # ------------------------------------------------------------ SpotReadVenue
    def server_time_ms(self) -> int:
        return int(self.clock() * 1000)

    def clock_skew_ms(self, samples: int = 5) -> int:
        return 0                                       # the simulator's clock is the exchange clock

    def symbol_rules(self, symbols: list[str]) -> dict[str, SymbolRules]:
        return self.market.symbol_rules(symbols)

    def books(self, symbols: list[str]) -> dict[str, Book]:
        return self.market.books(symbols)

    def quote_volume_24h(self, symbols: list[str]) -> dict[str, Decimal]:
        return self.market.quote_volume_24h(symbols)

    def ask_depth_quote(self, symbol: str, within_pct: Decimal = Decimal("0.5"), limit: int = 100) -> Decimal:
        return self.market.ask_depth_quote(symbol, within_pct, limit)

    def avg_price(self, symbol: str) -> Decimal:
        return self.market.avg_price(symbol)

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[Candle]:
        return self.market.klines(symbol, interval, start_ms, end_ms, limit)

    def balances(self) -> list[Balance]:
        """Coins held are shown as locked: the simulated exit orders reserve all of them."""
        out = [Balance(QUOTE, self.quote, ZERO)]
        out += [Balance(p.base_asset, ZERO, p.qty) for p in self.positions.values()]
        return out

    def taker_fee(self, symbol: str) -> Decimal:
        return self.fee_rate

    # ------------------------------------------------------------ account
    def equity(self) -> Decimal:
        """Equity at the last seen best bid. Raises MissingPrice if a held coin has no price."""
        return value_equity(self.balances(), self._bids, QUOTE)

    def _state(self, equity: Decimal) -> AccountState:
        mode = self.modes.mode
        if mode is Mode.RUNNING and not self.modes.entries_allowed:
            mode = Mode.ENTRIES_BLOCKED
        positions = tuple(
            OpenPosition(symbol=p.symbol, qty=p.qty, avg_entry=p.entry_price, stop_price=p.stop_price,
                         current_bid=self._bids[p.symbol], corr_group=p.corr_group)
            for p in self.positions.values()
        )
        return AccountState(
            mode=mode, equity=equity, free_quote=self.quote,
            day_start_equity=self.periods.day_start_equity, week_start_equity=self.periods.week_start_equity,
            daily_pnl=self.periods.daily_pnl(equity), weekly_pnl=self.periods.weekly_pnl(equity),
            drawdown_pct=self.index.drawdown_pct, positions=positions, taker_fee_rate=self.fee_rate,
        )

    def account_state(self, now: float | None = None) -> AccountState:
        """Mark equity (P&L periods, drawdown, breakers) and return the state the risk engine sees."""
        now = self.clock() if now is None else now
        equity = self.equity()
        events = self.periods.update(as_utc(now), equity)
        self.index.mark(equity)
        state = self._state(equity)
        if "day" in events:
            self.modes.on_day_rollover(state, as_utc(now))
        if "week" in events:
            self.modes.on_week_rollover(state)
        self.modes.on_state(state, as_utc(now))
        return self._state(equity)

    def resume(self, operator: str, reason: str, now: float | None = None) -> None:
        """Allow entries. Same rules as the real bot: a name, a reason, and no active breach."""
        now = self.clock() if now is None else now
        self.modes.resume(operator, reason, self.account_state(now), as_utc(now))

    # ------------------------------------------------------------ entries
    def submit(self, intent: TradeIntent, now: float | None = None) -> EntryResult:
        """The only way to open a simulated position. Every intent goes through evaluate()."""
        now = self.clock() if now is None else now
        try:
            if self.positions:
                self.poll(now)                         # exits first, so the state is current
            snapshot = self.data.snapshot(intent.symbol)
            rules = self.data.rules(intent.symbol)
        except MarketDataUnavailable as e:
            d = Decision.reject(RejectCode.STALE_DATA, f"market data unavailable: {e}")
            return EntryResult(d, False, reason=d.reason)
        state = self.account_state(now)
        decision = evaluate(intent, state, snapshot, rules, self.cfg, corr_group(intent.symbol))
        if not decision.accepted:
            return EntryResult(decision, False, reason=decision.reason)
        return self._open_position(intent, decision, snapshot, rules, now)

    def _open_position(self, intent: TradeIntent, decision: Decision, snapshot: MarketSnapshot,
                       rules: SymbolRules, now: float) -> EntryResult:
        entry = D(decision.details["entry"])
        stop = D(decision.details["stop"])
        take_profit = D(decision.details["take_profit"])
        qty = decision.qty
        worst_entry = entry * (1 + pct(self.cfg.risk.entry_slippage_pct))   # what the risk engine sized for
        price = fills.buy_fill_price(snapshot.best_ask, self._entry_slippage())
        if snapshot.best_ask <= stop:
            return EntryResult(decision, False, reason="not filled: the ask is already at or below the stop")
        if price > worst_entry:
            return EntryResult(decision, False,
                               reason=f"not filled: fill price {fmt(price)} is above the worst planned entry {fmt(worst_entry)}")
        fee = fills.taker_fee(qty * price, self.fee_rate)
        if qty * price + fee > self.quote:
            return EntryResult(decision, False, reason="not filled: not enough simulated USDT")

        intent_id = new_intent_id()
        self.quote -= qty * price + fee
        fill = Fill(client_order_id(intent_id, "entry"), intent.symbol, "BUY", "ENTRY", qty, price, fee, now,
                    slippage=qty * (price - snapshot.best_ask))
        self.fills.append(fill)
        self.positions[intent.symbol] = SimPosition(
            intent_id=intent_id, symbol=intent.symbol, base_asset=rules.base_asset, strategy=intent.strategy,
            qty=qty, entry_price=price, entry_fee=fee, stop_price=stop, take_profit_price=take_profit,
            planned_risk=decision.planned_risk, opened_at=now, expires_at=now + intent.horizon_hours * 3600,
            corr_group=corr_group(intent.symbol),
        )
        self._bids[intent.symbol] = snapshot.best_bid
        self.account_state(now)
        return EntryResult(decision, True, fill, reason=f"filled {fmt(qty)} {intent.symbol} at {fmt(price)}")

    # ------------------------------------------------------------ exits
    def poll(self, now: float | None = None) -> list[ClosedTrade]:
        """Fetch the latest books for every held coin and fill any exit they trigger."""
        now = self.clock() if now is None else now
        if not self.positions:
            return []
        books = self.data.books(list(self.positions))
        closed = [t for s, b in books.items() if (t := self._on_bid(s, b.bid, now)) is not None]
        self.account_state(now)
        return closed

    def raise_stop(self, symbol: str, new_stop: Decimal, now: float | None = None) -> bool:
        """Tighten a position's stop (trailing). Never lowers it, and never sets it at or above the
        current bid (that would sell at once) or the take-profit. Returns whether it changed."""
        pos = self.positions.get(symbol)
        new_stop = D(new_stop)
        if pos is None or new_stop <= pos.stop_price:
            return False
        bid = self._bids.get(symbol)
        if bid is None or new_stop >= bid or new_stop >= pos.take_profit_price:
            return False
        self.positions[symbol] = replace(pos, stop_price=new_stop)
        return True

    def close_position(self, symbol: str, now: float | None = None) -> ClosedTrade | None:
        """The strategy's own exit: sell the whole position at market (bid minus exit slippage).
        Selling only reduces risk, so this needs no risk-engine approval."""
        now = self.clock() if now is None else now
        if symbol not in self.positions:
            return None
        bid = self.data.books([symbol])[symbol].bid       # MarketDataUnavailable propagates: no price, no sale
        trade = self._on_bid(symbol, bid, now)            # a stop or target already hit takes precedence
        if trade is None and symbol in self.positions:
            trade = self._close(self.positions[symbol], fills.STRATEGY_EXIT, bid, now)
        self._mark_quietly(now)
        return trade

    def on_book(self, symbol: str, book: Book, now: float | None = None) -> ClosedTrade | None:
        now = self.clock() if now is None else now
        trade = self._on_bid(symbol, book.bid, now)
        self._mark_quietly(now)
        return trade

    def on_candle(self, symbol: str, candle: Candle) -> ClosedTrade | None:
        """Replay one finished candle. Candles that started before the entry are ignored:
        their high or low may have happened before the position existed."""
        if not candle.closed:
            return None
        now = candle.close_time / 1000
        pos = self.positions.get(symbol)
        if pos is None or candle.open_time < pos.opened_at * 1000:
            if pos is not None:
                self._bids[symbol] = candle.close
            self._mark_quietly(now)
            return None
        self._bids[symbol] = candle.close
        hit = fills.candle_exit(candle.open, candle.high, candle.low, pos.stop_price, pos.take_profit_price)
        if hit is None and now >= pos.expires_at:
            hit = (fills.TIME_STOP, candle.close)
        trade = self._close(pos, hit[0], hit[1], now) if hit else None
        self._mark_quietly(now)
        return trade

    def _on_bid(self, symbol: str, bid: Decimal, now: float) -> ClosedTrade | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        self._bids[symbol] = bid
        hit = fills.book_exit(bid, pos.stop_price, pos.take_profit_price)
        if hit is None and now >= pos.expires_at:
            hit = (fills.TIME_STOP, bid)
        return self._close(pos, hit[0], hit[1], now) if hit else None

    def _close(self, pos: SimPosition, reason: str, observed: Decimal, now: float) -> ClosedTrade:
        slip = self._exit_slippage(pos.symbol)
        if reason == fills.STOP_LOSS:
            price = fills.stop_fill_price(pos.stop_price, observed, slip)
            before_slip = min(pos.stop_price, observed)
            cid = client_order_id(pos.intent_id, "protect", 0)
        elif reason == fills.TAKE_PROFIT:
            price = before_slip = pos.take_profit_price
            cid = client_order_id(pos.intent_id, "protect", 1)
        else:
            price = fills.sell_fill_price(observed, slip)
            before_slip = observed
            cid = client_order_id(pos.intent_id, "exit")
        proceeds = pos.qty * price
        fee = fills.taker_fee(proceeds, self.fee_rate)
        self.quote += proceeds - fee
        pnl = proceeds - fee - (pos.qty * pos.entry_price + pos.entry_fee)
        self.fills.append(Fill(cid, pos.symbol, "SELL", reason, pos.qty, price, fee, now,
                               slippage=pos.qty * (before_slip - price)))
        trade = ClosedTrade(
            symbol=pos.symbol, strategy=pos.strategy, qty=pos.qty, entry_price=pos.entry_price, exit_price=price,
            exit_reason=reason, entry_fee=pos.entry_fee, exit_fee=fee, pnl=pnl, planned_risk=pos.planned_risk,
            gapped=reason == fills.STOP_LOSS and observed < pos.stop_price,
            opened_at=pos.opened_at, closed_at=now,
        )
        self.trades.append(trade)
        del self.positions[pos.symbol]
        return trade

    # ------------------------------------------------------------ helpers
    def _mark_quietly(self, now: float) -> None:
        try:
            self.account_state(now)
        except MissingPrice:
            pass        # a held coin has no price yet; the next update marks it

    def _entry_slippage(self) -> Decimal:
        base = D(self.entry_slippage_pct) if self.entry_slippage_pct is not None else self.cfg.risk.entry_slippage_pct
        return base * D(self.slippage_multiplier)

    def _exit_slippage(self, symbol: str) -> Decimal:
        base = (D(self.exit_slippage_pct) if self.exit_slippage_pct is not None
                else self.cfg.risk.stop_slippage_for(symbol))
        return base * D(self.slippage_multiplier)
