"""Parsers for Binance Spot read-only responses. Pure: plain dicts in, typed data out.

Input is the JSON shape Binance documents for each endpoint, so the same code
works on SDK responses, recorded fixtures and the simulator in module 3.
A field that is missing or malformed raises ExchangeDataError: the caller must
treat that as "no data" and block new entries (fail closed), never as zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from bot.core.money import ZERO, D
from bot.portfolio.accounting import Balance

# GET /api/v3/klines `interval` values (case-sensitive).
KLINE_INTERVALS = frozenset({
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M",
})


class ExchangeDataError(ValueError):
    """A Binance response was missing a field or had an impossible value."""


def _dec(obj: dict[str, Any], key: str, where: str) -> Decimal:
    try:
        raw = obj[key]
    except (KeyError, TypeError):
        raise ExchangeDataError(f"{where}: missing field {key!r}") from None
    if raw is None or isinstance(raw, bool):
        raise ExchangeDataError(f"{where}: field {key!r} is {raw!r}")
    try:
        value = D(raw)
    except (InvalidOperation, ValueError):
        raise ExchangeDataError(f"{where}: field {key!r} is not a number: {raw!r}") from None
    if not value.is_finite():
        raise ExchangeDataError(f"{where}: field {key!r} is not finite")
    return value


@dataclass(frozen=True)
class BookTicker:
    """Best bid/ask from GET /api/v3/ticker/bookTicker.

    The REST response carries no timestamp, so received_ms is the local time it
    arrived; data age for R-12 is measured from it.
    """
    symbol: str
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal
    received_ms: int

    @property
    def spread_pct(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


@dataclass(frozen=True)
class Kline:
    """One candle from GET /api/v3/klines. Times are UTC milliseconds."""
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal
    trades: int


@dataclass(frozen=True)
class CommissionRates:
    """Rates as fractions (0.001 = 0.1%) from GET /api/v3/account/commission.

    maker/taker are standard + special + tax commission. The BNB discount is
    deliberately ignored: it depends on holding BNB, so sizing assumes the full rate.
    """
    symbol: str
    maker: Decimal
    taker: Decimal


@dataclass(frozen=True)
class AccountInfo:
    """Subset of GET /api/v3/account that the bot uses."""
    account_type: str
    can_trade: bool
    can_withdraw: bool
    permissions: tuple[str, ...]
    balances: tuple[Balance, ...]
    update_time: int


def parse_book_ticker(obj: dict[str, Any], received_ms: int) -> BookTicker:
    symbol = obj.get("symbol") if isinstance(obj, dict) else None
    where = f"bookTicker {symbol}"
    if not symbol:
        raise ExchangeDataError("bookTicker: missing symbol")
    bt = BookTicker(
        symbol=symbol,
        bid=_dec(obj, "bidPrice", where),
        bid_qty=_dec(obj, "bidQty", where),
        ask=_dec(obj, "askPrice", where),
        ask_qty=_dec(obj, "askQty", where),
        received_ms=received_ms,
    )
    # An empty book side comes back as price 0; that is "no price", not a price of zero.
    if bt.bid <= 0 or bt.ask <= 0:
        raise ExchangeDataError(f"{where}: empty book side (bid {bt.bid}, ask {bt.ask})")
    if bt.ask < bt.bid:
        raise ExchangeDataError(f"{where}: crossed book (bid {bt.bid} > ask {bt.ask})")
    if bt.bid_qty < 0 or bt.ask_qty < 0:
        raise ExchangeDataError(f"{where}: negative quantity")
    return bt


def parse_kline(row: Any) -> Kline:
    if not isinstance(row, (list, tuple)) or len(row) < 9:
        raise ExchangeDataError(f"kline: expected a list of at least 9 fields, got {row!r}")
    fields = dict(enumerate(row))
    try:
        open_time, close_time, trades = int(row[0]), int(row[6]), int(row[8])
    except (TypeError, ValueError):
        raise ExchangeDataError(f"kline: bad time or trade count in {row!r}") from None
    k = Kline(
        open_time=open_time,
        open=_dec(fields, 1, "kline"),
        high=_dec(fields, 2, "kline"),
        low=_dec(fields, 3, "kline"),
        close=_dec(fields, 4, "kline"),
        volume=_dec(fields, 5, "kline"),
        close_time=close_time,
        quote_volume=_dec(fields, 7, "kline"),
        trades=trades,
    )
    if close_time <= open_time:
        raise ExchangeDataError(f"kline {open_time}: close time before open time")
    if not (k.low <= min(k.open, k.close) and max(k.open, k.close) <= k.high) or k.low <= 0:
        raise ExchangeDataError(f"kline {open_time}: prices are inconsistent")
    if k.volume < 0 or k.quote_volume < 0:
        raise ExchangeDataError(f"kline {open_time}: negative volume")
    return k


def parse_klines(rows: Any, now_ms: int | None = None) -> list[Kline]:
    """Parse and check ordering. With now_ms, the still-open last candle is dropped,
    so a strategy can never act on a candle that has not closed (no look-ahead)."""
    if not isinstance(rows, list):
        raise ExchangeDataError("klines: expected a list")
    out = [parse_kline(r) for r in rows]
    for a, b in zip(out, out[1:]):
        if b.open_time <= a.open_time:
            raise ExchangeDataError("klines: candles are not in ascending time order")
    if now_ms is not None:
        out = [k for k in out if k.close_time < now_ms]
    return out


def parse_commission(obj: dict[str, Any]) -> CommissionRates:
    symbol = obj.get("symbol", "") if isinstance(obj, dict) else ""
    where = f"commission {symbol}"
    if not isinstance(obj, dict) or "standardCommission" not in obj:
        raise ExchangeDataError(f"{where}: missing standardCommission")
    maker = taker = ZERO
    for part in ("standardCommission", "specialCommission", "taxCommission"):
        block = obj.get(part)
        if block is None and part != "standardCommission":
            continue
        maker += _dec(block, "maker", f"{where} {part}")
        taker += _dec(block, "taker", f"{where} {part}")
    if maker < 0 or taker < 0 or taker >= 1 or maker >= 1:
        raise ExchangeDataError(f"{where}: impossible rates maker={maker} taker={taker}")
    return CommissionRates(symbol=symbol, maker=maker, taker=taker)


def parse_balances(rows: Any) -> tuple[Balance, ...]:
    if not isinstance(rows, list):
        raise ExchangeDataError("account: balances is not a list")
    out = []
    for row in rows:
        asset = row.get("asset") if isinstance(row, dict) else None
        if not asset:
            raise ExchangeDataError("account: balance without asset")
        b = Balance(asset=asset, free=_dec(row, "free", f"balance {asset}"), locked=_dec(row, "locked", f"balance {asset}"))
        if b.free < 0 or b.locked < 0:
            raise ExchangeDataError(f"balance {asset}: negative amount")
        out.append(b)
    return tuple(out)


def parse_account(obj: dict[str, Any]) -> AccountInfo:
    if not isinstance(obj, dict) or "balances" not in obj:
        raise ExchangeDataError("account: missing balances")
    return AccountInfo(
        account_type=str(obj.get("accountType", "UNKNOWN")),
        can_trade=bool(obj.get("canTrade", False)),
        can_withdraw=bool(obj.get("canWithdraw", False)),
        permissions=tuple(obj.get("permissions") or ()),
        balances=parse_balances(obj["balances"]),
        update_time=int(obj.get("updateTime") or 0),
    )


def clock_skew_ms(local_before_ms: int, server_ms: int, local_after_ms: int) -> int:
    """|server - local| using the midpoint of the request, as R-13 needs."""
    if local_after_ms < local_before_ms:
        raise ExchangeDataError("local clock went backwards during the time request")
    midpoint = (local_before_ms + local_after_ms) // 2
    return abs(int(server_ms) - midpoint)
