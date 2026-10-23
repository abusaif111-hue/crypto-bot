"""Read-only Binance Spot adapter (module 2).

This class can only READ: prices, exchange rules, fees, balances and the API
key's own permissions. It has no method that places, changes or cancels an
order, and every call goes through `_get`, which only sends GET requests to the
endpoints listed in READ_ENDPOINTS (enforced by tests/unit/test_architecture.py).

Public market data uses Binance's market-data-only host; account calls use the
main API host and are signed with the Ed25519 key through the official SDK
(binance-sdk-spot).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from bot.core.money import ZERO, D
from bot.exchange.credentials import Credentials
from bot.exchange.filters import SymbolRules, parse_symbol
from bot.exchange.interface import Book, Candle  # noqa: F401  (re-exported)
from bot.portfolio.accounting import Balance

HOSTS = {
    "live": {"api": "https://api.binance.com", "data": "https://data-api.binance.vision"},
    "testnet": {"api": "https://testnet.binance.vision", "data": "https://testnet.binance.vision"},
}

# Every endpoint this adapter may call, and whether it needs a signature.
READ_ENDPOINTS: dict[str, bool] = {
    "/api/v3/time": False,
    "/api/v3/exchangeInfo": False,
    "/api/v3/ticker/bookTicker": False,
    "/api/v3/ticker/24hr": False,
    "/api/v3/depth": False,
    "/api/v3/klines": False,
    "/api/v3/avgPrice": False,
    "/api/v3/account": True,
    "/api/v3/account/commission": True,
    "/api/v3/myFilters": True,
    "/sapi/v1/account/apiRestrictions": True,
}

# Permissions that must be OFF on a read-only key. Any of these on is a hard stop.
FORBIDDEN_PERMISSIONS = (
    "enableWithdrawals", "enableSpotAndMarginTrading", "enableMargin", "enableFutures",
    "enableVanillaOptions", "enablePortfolioMarginTrading", "enableFixApiTrade",
)
# Permissions that should be off but only move money between your own Binance
# wallets; reported as warnings.
WARN_PERMISSIONS = ("enableInternalTransfer", "permitsUniversalTransfer")
ALLOWED_PERMISSIONS = ("enableReading", "enableFixReadOnly")

WEIGHT_SOFT_LIMIT = 4000   # Binance's limit is 6,000 request weight per minute; slow down well before it


class ExchangeError(RuntimeError):
    pass


class _Response(Protocol):
    headers: dict

    def data(self) -> Any: ...


class _Client(Protocol):
    """The two generic calls of binance_sdk_spot's SpotRestAPI that this adapter uses."""

    def send_request(self, endpoint: str, method: str, query_params: dict | None = None) -> _Response: ...

    def send_signed_request(self, endpoint: str, method: str, query_params: dict | None = None) -> _Response: ...


@dataclass(frozen=True)
class KeyPermissions:
    raw: dict[str, Any]
    problems: tuple[str, ...]       # hard stops
    warnings: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.problems

    @staticmethod
    def classify(raw: dict[str, Any]) -> "KeyPermissions":
        problems, warnings = [], []
        if raw.get("enableReading") is not True:
            problems.append("Enable Reading is off, so the bot cannot read the account")
        if raw.get("ipRestrict") is not True:
            problems.append("the key is not restricted to trusted IPs")
        for name in FORBIDDEN_PERMISSIONS:
            if raw.get(name) is True:
                problems.append(f"{name} is ON")
        for name in WARN_PERMISSIONS:
            if raw.get(name) is True:
                warnings.append(f"{name} is on (moves funds only between your own Binance wallets); turn it off if Binance allows")
        known = set(FORBIDDEN_PERMISSIONS) | set(WARN_PERMISSIONS) | set(ALLOWED_PERMISSIONS)
        for name, value in raw.items():
            if (name.startswith("enable") or name.startswith("permits")) and name not in known and value is True:
                warnings.append(f"{name} is on (a permission this bot does not know); check it in Binance")
        return KeyPermissions(raw=raw, problems=tuple(problems), warnings=tuple(warnings))


@dataclass
class BinanceReadOnly:
    environment: str = "live"
    credentials: Credentials | None = None
    public_client: _Client | None = None
    signed_client: _Client | None = None
    clock: Callable[[], float] = time.time
    used_weight: int = 0
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def __post_init__(self) -> None:
        if self.environment not in HOSTS:
            raise ExchangeError(f"environment '{self.environment}' has no Binance host here (use live or testnet)")
        if self.public_client is None:
            self.public_client = _sdk_client(HOSTS[self.environment]["data"], None)
        if self.signed_client is None and self.credentials is not None:
            self.signed_client = _sdk_client(HOSTS[self.environment]["api"], self.credentials)

    # ------------------------------------------------------------ the only way out
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if endpoint not in READ_ENDPOINTS:
            raise ExchangeError(f"{endpoint} is not a read-only endpoint this adapter may call")
        signed = READ_ENDPOINTS[endpoint]
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.used_weight >= WEIGHT_SOFT_LIMIT:
            self._sleep(5)
        try:
            if signed:
                if self.signed_client is None:
                    raise ExchangeError("this call needs the API key; none is loaded")
                resp = self.signed_client.send_signed_request(endpoint, "GET", query_params=params)
            else:
                resp = self.public_client.send_request(endpoint, "GET", query_params=params)
        except ExchangeError:
            raise
        except Exception as e:  # SDK errors: keep the message, never the request (it carries the signature)
            raise ExchangeError(f"Binance {endpoint} failed: {type(e).__name__}: {getattr(e, 'error_message', None) or e}") from None
        headers = {k.lower(): v for k, v in (getattr(resp, "headers", None) or {}).items()}
        w = headers.get("x-mbx-used-weight-1m")
        if w is not None:
            try:
                self.used_weight = int(w)
            except ValueError:
                pass
        return resp.data()

    # ------------------------------------------------------------ public data
    def server_time_ms(self) -> int:
        return int(self._get("/api/v3/time")["serverTime"])

    def clock_skew_ms(self, samples: int = 5) -> int:
        """Local clock minus Binance server time.

        The first request also sets up the TLS connection, which makes its timing
        lopsided, so it is discarded. Of the remaining samples the one with the
        shortest round trip is used, compared at the middle of that round trip.
        """
        self.server_time_ms()                       # warm-up: opens the connection
        best: tuple[float, float] | None = None     # (round trip, skew)
        for _ in range(max(1, samples)):
            t0 = self.clock()
            server = self.server_time_ms()
            t1 = self.clock()
            rtt = t1 - t0
            skew = (t0 + t1) / 2 * 1000 - server
            if best is None or rtt < best[0]:
                best = (rtt, skew)
        return int(round(best[1]))

    def symbol_rules(self, symbols: list[str]) -> dict[str, SymbolRules]:
        info = self._get("/api/v3/exchangeInfo", {"symbols": json.dumps(symbols, separators=(",", ":"))})
        out = {}
        for s in info["symbols"]:
            my = self._get("/api/v3/myFilters", {"symbol": s["symbol"]}) if self.signed_client else None
            out[s["symbol"]] = parse_symbol(s, my)
        missing = set(symbols) - set(out)
        if missing:
            raise ExchangeError(f"Binance returned no rules for {sorted(missing)}")
        return out

    def all_symbol_rules(self) -> list[SymbolRules]:
        """Every symbol Binance lists, in one public exchangeInfo call (request weight 20).
        Symbols whose filters cannot be parsed are left out."""
        out = []
        for s in self._get("/api/v3/exchangeInfo")["symbols"]:
            try:
                out.append(parse_symbol(s))
            except ValueError:
                pass
        return out

    def books(self, symbols: list[str]) -> dict[str, Book]:
        data = self._get("/api/v3/ticker/bookTicker", {"symbols": json.dumps(symbols, separators=(",", ":"))})
        return {d["symbol"]: Book(D(d["bidPrice"]), D(d["askPrice"])) for d in data}

    def quote_volume_24h(self, symbols: list[str]) -> dict[str, Decimal]:
        data = self._get("/api/v3/ticker/24hr", {"symbols": json.dumps(symbols, separators=(",", ":"))})
        return {d["symbol"]: D(d["quoteVolume"]) for d in data}

    def ask_depth_quote(self, symbol: str, within_pct: Decimal = Decimal("0.5"), limit: int = 100) -> Decimal:
        """USDT available on the ask side within `within_pct` % of the best ask."""
        data = self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})
        asks = [(D(p), D(q)) for p, q in data.get("asks", [])]
        if not asks:
            return ZERO
        cap = asks[0][0] * (1 + within_pct / 100)
        return sum((p * q for p, q in asks if p <= cap), ZERO)

    def avg_price(self, symbol: str) -> Decimal:
        """Binance's 5-minute average price: the reference for PERCENT_PRICE_BY_SIDE."""
        return D(self._get("/api/v3/avgPrice", {"symbol": symbol})["price"])

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[Candle]:
        data = self._get("/api/v3/klines", {"symbol": symbol, "interval": interval, "startTime": start_ms,
                                            "endTime": end_ms, "limit": limit})
        now_ms = int(self.clock() * 1000)
        return [Candle(open_time=int(k[0]), open=D(k[1]), high=D(k[2]), low=D(k[3]), close=D(k[4]),
                       volume=D(k[5]), close_time=int(k[6]), quote_volume=D(k[7]), closed=int(k[6]) < now_ms)
                for k in data]

    # ------------------------------------------------------------ account (signed, read-only)
    def key_permissions(self) -> KeyPermissions:
        return KeyPermissions.classify(self._get("/sapi/v1/account/apiRestrictions"))

    def balances(self) -> list[Balance]:
        data = self._get("/api/v3/account", {"omitZeroBalances": "true"})
        return [Balance(b["asset"], D(b["free"]), D(b["locked"])) for b in data.get("balances", [])
                if D(b["free"]) + D(b["locked"]) > 0]

    def taker_fee(self, symbol: str) -> Decimal:
        """Taker commission as a fraction (0.001 = 0.1%), standard plus tax rate."""
        data = self._get("/api/v3/account/commission", {"symbol": symbol})
        std = data.get("standardCommission", {})
        tax = data.get("taxCommission", {})
        return D(std.get("taker", "0")) + D(tax.get("taker", "0"))

    def value_balances(self, balances: list[Balance], quote: str = "USDT") -> tuple[Decimal, list[str]]:
        """Equity at best bid. Assets without a {asset}USDT book are returned as unvalued, never as zero silently."""
        others = sorted({b.asset for b in balances if b.asset != quote})
        bids: dict[str, Decimal] = {}
        if others:
            for sym in (a + quote for a in others):   # one call each: an unknown symbol fails a batched call
                try:
                    bids[sym] = self.books([sym])[sym].bid
                except ExchangeError:
                    pass
        total, unvalued = ZERO, []
        for b in balances:
            if b.asset == quote:
                total += b.total
            elif (b.asset + quote) in bids and bids[b.asset + quote] > 0:
                total += b.total * bids[b.asset + quote]
            else:
                unvalued.append(b.asset)
        return total, unvalued


class _SdkGetClient:
    """GET-only wrapper over Binance's official SDK.

    binance-sdk-spot 13.2.0's own generic `send_request`/`send_signed_request`
    methods crash (they subscript a function), so this calls the same SDK helper
    that the SDK's named methods use, with the SDK's session, configuration and
    Ed25519 signer. Signing and error handling stay entirely in Binance's code.
    """

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def _call(self, endpoint: str, method: str, query_params: dict | None, signed: bool) -> _Response:
        from binance_common.utils import send_request

        if method != "GET":
            raise ExchangeError("the read-only client only sends GET requests")
        return send_request(self._sdk._session, self._sdk.configuration, method="GET", path=endpoint,
                            payload=query_params or None, body=None,
                            time_unit=self._sdk.configuration.time_unit,
                            is_signed=signed, signer=self._sdk._signer if signed else None)

    def send_request(self, endpoint: str, method: str, query_params: dict | None = None) -> _Response:
        return self._call(endpoint, method, query_params, signed=False)

    def send_signed_request(self, endpoint: str, method: str, query_params: dict | None = None) -> _Response:
        return self._call(endpoint, method, query_params, signed=True)


def _sdk_client(base_url: str, creds: Credentials | None) -> _Client:
    """Build Binance's official SDK client. Imported here so tests run without network."""
    from binance_common.configuration import ConfigurationRestAPI
    from binance_sdk_spot.rest_api import SpotRestAPI

    cfg = ConfigurationRestAPI(
        api_key=creds.api_key if creds else None,
        private_key=str(creds.private_key_path) if creds else None,
        base_path=base_url,
        timeout=10000,
        retries=2,
        backoff=1000,
    )
    return _SdkGetClient(SpotRestAPI(cfg))
