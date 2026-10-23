"""Binance Spot trading adapter (module 5a): single orders on the SPOT TESTNET only.

Separate from the read-only adapter (`bot.exchange.binance`), which stays read-only. This
class may call only the endpoints in TRADE_ENDPOINTS, and refuses any environment but the
testnet: live orders need Sultan's live switch, a later module. Only `bot.execution` (and
the `bot check-trading` command) may use it; every entry there goes through the risk engine.

Never two orders for one idea. Binance accepts a second order with the same client order ID
once the first has filled, so a blind retry could buy twice. When an order's outcome is
unknown (timeout, network error, Binance codes -1006/-1007 "execution status unknown"),
nothing is re-sent: the order is looked up by its client ID a few times, and if Binance
still cannot say, `OrderStatusUnknown` is raised and the caller must block new entries
until reconciliation settles it. The SDK's own retries are off for this client.

Checked against Binance's Spot API docs (rest-api.md, errors.md, enums.md; last updated
2026-09-18). Behaviour on the testnet is to be confirmed there (module 5 gate: 5 clean
round trips).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from bot.core.ids import is_valid_client_id
from bot.core.money import D, is_multiple
from bot.exchange.binance import HOSTS, ExchangeError
from bot.exchange.credentials import Credentials
from bot.exchange.filters import SymbolRules, limit_order_violations
from bot.portfolio.accounting import Balance

# (method, path): request weight, from Binance's docs.
TRADE_ENDPOINTS: dict[tuple[str, str], int] = {
    ("POST", "/api/v3/order"): 1,
    ("POST", "/api/v3/order/test"): 1,
    ("GET", "/api/v3/order"): 4,
    ("DELETE", "/api/v3/order"): 1,
    ("GET", "/api/v3/openOrders"): 6,
    ("GET", "/api/v3/account"): 20,
    ("GET", "/api/v3/myTrades"): 20,
}
ENVIRONMENTS = ("testnet",)            # live: only with Sultan's live switch (a later module)
AMBIGUOUS_CODES = {-1006, -1007}       # "execution status unknown"
NO_SUCH_ORDER = -2013
CANCEL_REJECTED = -2011
DUPLICATE_ORDER = "Duplicate order sent"
TIME_IN_FORCE = ("GTC", "IOC", "FOK")
FINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH")


class OrderRejected(ExchangeError):
    """Binance (or the pre-send check) definitely refused the order; nothing is open."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class OrderStatusUnknown(ExchangeError):
    """It is not known whether the order exists. Block new entries until reconciliation settles it."""

    def __init__(self, symbol: str, client_order_id: str, why: str):
        super().__init__(f"{symbol} order {client_order_id}: outcome unknown ({why}); new entries must stay "
                         f"blocked until reconciliation finds it")
        self.symbol, self.client_order_id = symbol, client_order_id


class _Ambiguous(Exception):
    pass


class _Refused(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class _TradeClient(Protocol):
    def send(self, method: str, endpoint: str, params: dict[str, Any]) -> Any: ...


# ---------------------------------------------------------------- orders
@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str                  # BUY or SELL
    type: str                  # LIMIT, or MARKET for a SELL (an exit) only
    qty: Decimal
    client_order_id: str
    price: Decimal | None = None
    time_in_force: str | None = None

    def problems(self, rules: SymbolRules) -> list[str]:
        out = []
        if self.symbol != rules.symbol:
            out.append(f"rules are for {rules.symbol}, not {self.symbol}")
        if self.side not in ("BUY", "SELL"):
            out.append(f"side {self.side!r}")
        if not is_valid_client_id(self.client_order_id):
            out.append("client order ID is not a valid Binance ID")
        if self.type == "LIMIT":
            if self.price is None or self.time_in_force not in TIME_IN_FORCE:
                out.append("a LIMIT order needs a price and GTC, IOC or FOK")
            else:
                out += limit_order_violations(rules, self.side, self.price, self.qty)
        elif self.type == "MARKET":
            if self.side != "SELL":
                out.append("market orders are for exits only: an entry is a LIMIT order with a price cap")
            if self.price is not None or self.time_in_force is not None:
                out.append("a MARKET order takes no price or time in force")
            step = rules.market_step_size or rules.step_size
            if self.qty <= 0 or not is_multiple(self.qty, step):
                out.append("quantity not a positive multiple of the market step size")
            if self.qty < (rules.market_min_qty or rules.min_qty):
                out.append("quantity below the market minimum")
        else:
            out.append(f"order type {self.type!r} is not supported here")
        return out

    def params(self) -> dict[str, Any]:
        p = {"symbol": self.symbol, "side": self.side, "type": self.type, "quantity": _fmt(self.qty),
             "newClientOrderId": self.client_order_id}
        if self.type == "LIMIT":
            p.update(price=_fmt(self.price), timeInForce=self.time_in_force)
        return p


@dataclass(frozen=True)
class TradeFill:
    price: Decimal
    qty: Decimal
    commission: Decimal
    commission_asset: str


@dataclass(frozen=True)
class OrderState:
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    type: str
    orig_qty: Decimal
    executed_qty: Decimal
    quote_qty: Decimal                     # USDT spent (BUY) or received (SELL) so far
    price: Decimal
    fills: tuple[TradeFill, ...] = ()

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

    @staticmethod
    def parse(d: dict[str, Any]) -> "OrderState":
        return OrderState(
            symbol=d["symbol"], order_id=int(d["orderId"]), client_order_id=d["clientOrderId"], status=d["status"],
            side=d["side"], type=d["type"], orig_qty=D(d["origQty"]), executed_qty=D(d["executedQty"]),
            quote_qty=D(d.get("cummulativeQuoteQty", "0")), price=D(d.get("price", "0")),
            fills=tuple(TradeFill(D(f["price"]), D(f["qty"]), D(f["commission"]), f["commissionAsset"])
                        for f in d.get("fills", ())))


@dataclass(frozen=True)
class AccountInfo:
    can_trade: bool
    can_withdraw: bool
    account_type: str
    permissions: tuple[str, ...]
    balances: tuple[Balance, ...]


@dataclass(frozen=True)
class TradingKeyCheck:
    problems: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.problems


def check_trading_account(info: AccountInfo, environment: str) -> TradingKeyCheck:
    problems, notes = [], []
    if not info.can_trade:
        problems.append("trading is not enabled for this key")
    if info.account_type != "SPOT":
        problems.append(f"account type is {info.account_type}, not SPOT")
    for p in info.permissions:
        if p in ("MARGIN", "LEVERAGED") or "FUTURES" in p:
            problems.append(f"the account has the {p} permission; this bot trades Spot only")
    if environment == "testnet":
        notes.append("testnet: key restrictions (withdrawals, trusted IPs) cannot be read here; testnet money is "
                     "not real. The live key's restrictions are checked before any live order (later module).")
    elif info.can_withdraw:
        problems.append("the account can withdraw with this key")
    return TradingKeyCheck(tuple(problems), tuple(notes))


# ---------------------------------------------------------------- the adapter
@dataclass
class BinanceTrader:
    environment: str = "testnet"
    credentials: Credentials | None = None
    client: _TradeClient | None = None
    confirm_attempts: int = 3
    confirm_wait_s: float = 2.0
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ExchangeError(f"orders are allowed only on the Binance testnet in this version, not "
                                f"'{self.environment}'; live trading needs Sultan's live switch (a later module)")
        if self.client is None:
            if self.credentials is None:
                raise ExchangeError("the trading adapter needs the testnet trading key (BINANCE_TESTNET_API_KEY)")
            self.client = _sdk_trade_client(HOSTS[self.environment]["api"], self.credentials)

    def _call(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if (method, endpoint) not in TRADE_ENDPOINTS:
            raise ExchangeError(f"{method} {endpoint} is not an endpoint the trading adapter may call")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            return self.client.send(method, endpoint, params)
        except (_Ambiguous, _Refused):
            raise
        except Exception as e:      # SDK errors: keep the code and message, never the request (it is signed)
            code = getattr(e, "status_code", None)
            msg = str(getattr(e, "error_message", None) or e)
            name = type(e).__name__
            if code in AMBIGUOUS_CODES or name in ("NetworkError", "ServerError") or name not in _DEFINITE:
                raise _Ambiguous(f"{name}: {msg}") from None
            raise _Refused(code if isinstance(code, int) else None, f"{name}: {msg}") from None

    # ------------------------------------------------------------ orders
    def place(self, req: OrderRequest, rules: SymbolRules) -> OrderState:
        """Send one order. Never sends it twice; see the module docstring."""
        bad = req.problems(rules)
        if bad:
            raise OrderRejected("not sent: " + "; ".join(bad))
        try:
            data = self._call("POST", "/api/v3/order", {**req.params(), "newOrderRespType": "FULL"})
        except _Ambiguous as e:
            return self._confirm(req, str(e))
        except _Refused as e:
            if DUPLICATE_ORDER.lower() in e.message.lower():     # sent before: find it, never send again
                return self._confirm(req, "Binance reports a duplicate client order ID")
            raise OrderRejected(f"Binance refused the order: {e.message}", e.code) from None
        return OrderState.parse(data)

    def test_order(self, req: OrderRequest, rules: SymbolRules) -> None:
        """Binance validates the order without trading (POST /api/v3/order/test)."""
        bad = req.problems(rules)
        if bad:
            raise OrderRejected("not sent: " + "; ".join(bad))
        try:
            self._call("POST", "/api/v3/order/test", req.params())
        except _Ambiguous as e:
            raise ExchangeError(f"test order: no answer from Binance ({e})") from None
        except _Refused as e:
            raise OrderRejected(f"Binance refused the test order: {e.message}", e.code) from None

    def _confirm(self, req: OrderRequest, why: str) -> OrderState:
        for _ in range(self.confirm_attempts):
            self._sleep(self.confirm_wait_s)
            try:
                found = self.query(req.symbol, req.client_order_id)
            except ExchangeError:
                continue
            if found is not None:
                return found
        raise OrderStatusUnknown(req.symbol, req.client_order_id, why)

    def query(self, symbol: str, client_order_id: str) -> OrderState | None:
        """The order by its client ID, or None if Binance says it does not exist."""
        try:
            return OrderState.parse(self._call("GET", "/api/v3/order",
                                               {"symbol": symbol, "origClientOrderId": client_order_id}))
        except _Refused as e:
            if e.code == NO_SUCH_ORDER:
                return None
            raise ExchangeError(f"query {symbol} {client_order_id}: {e.message}") from None
        except _Ambiguous as e:
            raise ExchangeError(f"query {symbol} {client_order_id}: {e}") from None

    def cancel(self, symbol: str, client_order_id: str) -> OrderState | None:
        """Cancel by client ID. If Binance refuses (already filled or gone), returns the order as it is now."""
        try:
            return OrderState.parse(self._call("DELETE", "/api/v3/order",
                                               {"symbol": symbol, "origClientOrderId": client_order_id}))
        except _Refused as e:
            if e.code in (CANCEL_REJECTED, NO_SUCH_ORDER):
                return self.query(symbol, client_order_id)
            raise ExchangeError(f"cancel {symbol} {client_order_id}: {e.message}") from None
        except _Ambiguous:
            return self.query(symbol, client_order_id)

    def open_orders(self, symbol: str | None = None) -> list[OrderState]:
        return [OrderState.parse(d) for d in self._read("GET", "/api/v3/openOrders", {"symbol": symbol})]

    def my_trades(self, symbol: str, order_id: int) -> list[TradeFill]:
        rows = self._read("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id})
        return [TradeFill(D(r["price"]), D(r["qty"]), D(r["commission"]), r["commissionAsset"]) for r in rows]

    def account(self) -> AccountInfo:
        d = self._read("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return AccountInfo(
            can_trade=bool(d.get("canTrade")), can_withdraw=bool(d.get("canWithdraw")),
            account_type=str(d.get("accountType", "")), permissions=tuple(d.get("permissions", ())),
            balances=tuple(Balance(b["asset"], D(b["free"]), D(b["locked"])) for b in d.get("balances", ())
                           if D(b["free"]) + D(b["locked"]) > 0))

    def _read(self, method: str, endpoint: str, params: dict[str, Any]) -> Any:
        try:
            return self._call(method, endpoint, params)
        except (_Ambiguous, _Refused) as e:
            raise ExchangeError(f"{endpoint}: {e}") from None


_DEFINITE = {"BadRequestError", "UnauthorizedError", "ForbiddenError", "NotFoundError", "TooManyRequestsError",
             "RateLimitBanError", "RequiredError"}


def _fmt(x: Decimal | None) -> str:
    """Plain decimal text for Binance (no exponent): 0.00010 -> '0.0001', 1E+1 -> '10'."""
    return format(D(x).normalize(), "f")


class _SdkTradeClient:
    """Signed calls through Binance's official SDK helper (as the read-only adapter does), with the SDK's
    own retries off: this adapter decides what happens when an outcome is unknown."""

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def send(self, method: str, endpoint: str, params: dict[str, Any]) -> Any:
        from binance_common.utils import send_request

        resp = send_request(self._sdk._session, self._sdk.configuration, method=method, path=endpoint,
                            payload=params or None, body=None, time_unit=self._sdk.configuration.time_unit,
                            is_signed=True, signer=self._sdk._signer)
        return resp.data()


def _sdk_trade_client(base_url: str, creds: Credentials) -> _TradeClient:
    from binance_common.configuration import ConfigurationRestAPI
    from binance_sdk_spot.rest_api import SpotRestAPI

    cfg = ConfigurationRestAPI(api_key=creds.api_key, private_key=str(creds.private_key_path), base_path=base_url,
                               timeout=10000, retries=0, backoff=1000)
    return _SdkTradeClient(SpotRestAPI(cfg))
