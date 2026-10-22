"""Read-only Binance Spot adapter (module 2), built on the official binance-sdk-spot.

It can read exchange rules, account filters, commission rates, balances,
candles, best bid/ask and server time. It cannot place, cancel or amend an
order: every SDK call goes through `_call`, which only accepts the GET
endpoints in READ_ONLY_CALLS, and tests/unit/test_architecture.py checks that
no source file names an order endpoint. Order sending arrives in module 5,
behind the risk engine, as separate code.

Failure handling is fail closed: any SDK, network or rate-limit error becomes
ExchangeUnavailable, a malformed payload becomes ExchangeDataError, and the
caller must block new entries on either. After a 429/418 or when the used
request weight passes WEIGHT_GUARD of the limit, calls are refused locally
until the window resets, so the bot backs off instead of earning an IP ban.

Not yet verified against a live connection; run `bot exchange-check` against
testnet first (verify on testnet).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from binance_common.errors import RateLimitBanError, TooManyRequestsError

from bot.core.config import Environment
from bot.exchange.filters import SymbolRules, parse_symbol
from bot.exchange.market import (
    KLINE_INTERVALS,
    AccountInfo,
    BookTicker,
    CommissionRates,
    ExchangeDataError,
    Kline,
    clock_skew_ms,
    parse_account,
    parse_book_ticker,
    parse_commission,
    parse_klines,
)

# The only SDK methods this adapter may call. Each is a GET endpoint:
#   time               GET /api/v3/time                 weight 1
#   exchange_info      GET /api/v3/exchangeInfo         weight 20
#   ticker_book_ticker GET /api/v3/ticker/bookTicker    weight 2-4
#   klines             GET /api/v3/klines               weight 2
#   get_account        GET /api/v3/account              weight 20 (signed)
#   account_commission GET /api/v3/account/commission   weight 20 (signed)
#   my_filters         GET /api/v3/myFilters            weight 40 (signed)
PUBLIC_CALLS = frozenset({"time", "exchange_info", "ticker_book_ticker", "klines"})
SIGNED_CALLS = frozenset({"get_account", "account_commission", "my_filters"})
READ_ONLY_CALLS = PUBLIC_CALLS | SIGNED_CALLS

# Official SDK base URLs, as in binance_common.constants.
BASE_URLS: dict[Environment, str] = {
    Environment.TESTNET: "https://testnet.binance.vision",
    Environment.DEMO: "https://demo-api.binance.com",
    Environment.LIVE: "https://api.binance.com",
    Environment.PAPER: "https://api.binance.com",   # paper trades on real market data
    Environment.SIM: "https://api.binance.com",
}

DEFAULT_WEIGHT_LIMIT_1M = 6000      # REQUEST_WEIGHT per minute; replaced by exchangeInfo's value when read
WEIGHT_GUARD = Decimal("0.8")       # stop calling at 80% of the limit
MAX_KLINES = 1000                   # Binance maximum per request


class ExchangeError(RuntimeError):
    """Base class: any of these means "no trustworthy data", so block new entries."""


class ExchangeUnavailable(ExchangeError):
    """Network, server, auth or rate-limit failure, or a call refused by the local guard."""


class MissingCredentials(ExchangeError):
    """A signed endpoint was requested but no API key is configured."""


@dataclass(frozen=True)
class Credentials:
    """API key and the path of an Ed25519 private key file outside the repository.

    The key text itself is never stored here and never printed.
    """
    api_key: str
    private_key_path: Path

    def __repr__(self) -> str:
        return f"Credentials(api_key='***', private_key_path='{self.private_key_path}')"

    __str__ = __repr__


def load_credentials(environ: Mapping[str, str] | None = None, repo_root: Path | None = None) -> Credentials | None:
    """Read BINANCE_API_KEY and BINANCE_PRIVATE_KEY_PATH. None if no key is set (public data only).

    Refuses a private key stored inside the repository, where it could be committed.
    """
    environ = os.environ if environ is None else environ
    api_key = (environ.get("BINANCE_API_KEY") or "").strip()
    key_path = (environ.get("BINANCE_PRIVATE_KEY_PATH") or "").strip()
    if not api_key:
        return None
    if not key_path:
        raise MissingCredentials("BINANCE_API_KEY is set but BINANCE_PRIVATE_KEY_PATH is not")
    path = Path(key_path).expanduser().resolve()
    if repo_root is not None and path.is_relative_to(Path(repo_root).resolve()):
        raise MissingCredentials("the private key file is inside the repository; move it outside")
    if not path.is_file():
        raise MissingCredentials(f"private key file not found: {path}")
    return Credentials(api_key=api_key, private_key_path=path)


def build_sdk_client(env: Environment, credentials: Credentials | None, timeout_ms: int = 5000) -> Any:
    """Construct the official SDK REST client. Only called when a real connection is wanted."""
    from binance_common.configuration import ConfigurationRestAPI
    from binance_sdk_spot.spot import Spot

    kwargs: dict[str, Any] = {"base_path": BASE_URLS[env], "timeout": timeout_ms, "retries": 2, "backoff": 1000}
    if credentials is not None:
        kwargs["api_key"] = credentials.api_key
        kwargs["private_key"] = str(credentials.private_key_path)
    return Spot(config_rest_api=ConfigurationRestAPI(**kwargs)).rest_api


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _plain(data: Any) -> Any:
    """SDK model -> the JSON shape Binance documents. Raw payloads pass through.

    Recursive because some SDK models (e.g. a multi-symbol bookTicker) return a
    list of models from to_dict().
    """
    if hasattr(data, "to_dict") and callable(data.to_dict):
        return _plain(data.to_dict())
    if isinstance(data, list):
        return [_plain(x) for x in data]
    if isinstance(data, dict):
        return {k: _plain(v) for k, v in data.items()}
    return data


class BinanceReadOnly:
    """Read-only view of Binance Spot. `rest` is the SDK's SpotRestAPI (or a test double)."""

    def __init__(self, rest: Any, *, signed: bool, clock: Callable[[], int] = _now_ms) -> None:
        self.__rest = rest
        self._signed = signed
        self._clock = clock
        self._weight_limit_1m = DEFAULT_WEIGHT_LIMIT_1M
        self.used_weight_1m: int | None = None
        self._blocked_until_ms = 0

    @classmethod
    def connect(cls, env: Environment, credentials: Credentials | None = None) -> "BinanceReadOnly":
        return cls(build_sdk_client(env, credentials), signed=credentials is not None)

    def __repr__(self) -> str:
        return f"BinanceReadOnly(signed={self._signed})"

    # ------------------------------------------------------------ plumbing
    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in READ_ONLY_CALLS:
            raise PermissionError(f"{name} is not a read-only endpoint; this adapter cannot call it")
        if name in SIGNED_CALLS and not self._signed:
            raise MissingCredentials(f"{name} needs an API key (set BINANCE_API_KEY)")
        now = self._clock()
        if now < self._blocked_until_ms:
            raise ExchangeUnavailable(f"rate-limit guard: calls paused for {(self._blocked_until_ms - now) // 1000 + 1}s")
        try:
            resp = getattr(self.__rest, name)(*args, **kwargs)
            data = resp.data()
        except Exception as e:  # fail closed on anything the SDK or network throws
            if isinstance(e, (TooManyRequestsError, RateLimitBanError)):
                wait_ms = int(e.retry_after) * 1000 if e.retry_after else 60_000
                self._blocked_until_ms = self._clock() + wait_ms
            raise ExchangeUnavailable(f"{name} failed: {type(e).__name__}: {e}") from e
        self._track_weight(resp)
        return _plain(data)

    @staticmethod
    def _next_minute(ms: int) -> int:
        return (ms // 60_000 + 1) * 60_000

    def _track_weight(self, resp: Any) -> None:
        for rl in getattr(resp, "rate_limits", None) or ():
            if (getattr(rl, "rateLimitType", None) == "REQUEST_WEIGHT" and getattr(rl, "interval", None) == "MINUTE"
                    and getattr(rl, "intervalNum", None) == 1):
                self.used_weight_1m = int(rl.count)
                if self.used_weight_1m >= WEIGHT_GUARD * self._weight_limit_1m:
                    self._blocked_until_ms = self._next_minute(self._clock())

    # ------------------------------------------------------------ public data
    def server_time_ms(self) -> int:
        data = self._call("time")
        try:
            return int(data["serverTime"])
        except (KeyError, TypeError, ValueError):
            raise ExchangeDataError("time: missing serverTime") from None

    def clock_skew_ms(self) -> int:
        """|local clock - Binance server time| for R-13."""
        before = self._clock()
        server = self.server_time_ms()
        return clock_skew_ms(before, server, self._clock())

    def symbol_rules(self, symbols: Iterable[str], account_filters: bool | None = None) -> dict[str, SymbolRules]:
        """Exchange rules for each symbol. Raises if any is missing, not USDT-quoted or not Spot.

        account_filters (default: when signed) also reads GET /api/v3/myFilters per
        symbol, the only source of the account's MAX_ASSET limits.
        """
        wanted = sorted(set(symbols))
        if not wanted:
            raise ValueError("no symbols requested")
        info = self._call("exchange_info", symbols=wanted)
        if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
            raise ExchangeDataError("exchangeInfo: missing symbols")
        for rl in info.get("rateLimits") or ():
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE" and rl.get("intervalNum") == 1:
                self._weight_limit_1m = int(rl["limit"])
        by_symbol = {s.get("symbol"): s for s in info["symbols"]}
        use_account = self._signed if account_filters is None else account_filters
        out: dict[str, SymbolRules] = {}
        for sym in wanted:
            raw = by_symbol.get(sym)
            if raw is None:
                raise ExchangeDataError(f"exchangeInfo: {sym} not returned")
            if raw.get("quoteAsset") != "USDT":
                raise ExchangeDataError(f"{sym}: quote asset is {raw.get('quoteAsset')}, only USDT is allowed")
            if raw.get("isSpotTradingAllowed") is False:
                raise ExchangeDataError(f"{sym}: Spot trading is not allowed on this symbol")
            my = self._call("my_filters", symbol=sym) if use_account else None
            try:
                out[sym] = parse_symbol(raw, my_filters=my)
            except (KeyError, TypeError, ValueError) as e:
                raise ExchangeDataError(f"exchangeInfo {sym}: {e}") from e
        return out

    def book_tickers(self, symbols: Iterable[str]) -> dict[str, BookTicker]:
        wanted = sorted(set(symbols))
        if not wanted:
            raise ValueError("no symbols requested")
        data = self._call("ticker_book_ticker", symbols=wanted)
        received = self._clock()
        rows = data if isinstance(data, list) else [data]
        out = {bt.symbol: bt for bt in (parse_book_ticker(r, received) for r in rows)}
        missing = set(wanted) - out.keys()
        if missing:
            raise ExchangeDataError(f"bookTicker: no data for {sorted(missing)}")
        return {s: out[s] for s in wanted}

    def klines(self, symbol: str, interval: str, *, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500, closed_only: bool = True) -> list[Kline]:
        """Candles in ascending time. closed_only drops the still-forming last candle."""
        if interval not in KLINE_INTERVALS:
            raise ValueError(f"unsupported kline interval {interval!r}")
        if not 1 <= limit <= MAX_KLINES:
            raise ValueError(f"limit must be 1-{MAX_KLINES}")
        rows = self._call("klines", symbol=symbol, interval=interval, start_time=start_ms, end_time=end_ms, limit=limit)
        return parse_klines(rows, now_ms=self._clock() if closed_only else None)

    # ------------------------------------------------------------ account (signed, read-only)
    def account(self) -> AccountInfo:
        return parse_account(self._call("get_account", omit_zero_balances=True))

    def commission(self, symbol: str) -> CommissionRates:
        rates = parse_commission(self._call("account_commission", symbol=symbol))
        if rates.symbol and rates.symbol != symbol:
            raise ExchangeDataError(f"commission: asked for {symbol}, got {rates.symbol}")
        return CommissionRates(symbol=symbol, maker=rates.maker, taker=rates.taker)
