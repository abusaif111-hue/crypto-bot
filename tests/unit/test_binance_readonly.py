"""The read-only adapter, driven by a fake SDK client that returns real SDK response models.

No network: the fake builds each response the way binance-sdk-spot does
(model.from_dict on the documented JSON, raw lists for klines), so these tests
check the adapter against the SDK's actual model classes.
"""
from decimal import Decimal as Dec
from types import SimpleNamespace

import pytest
from binance_common.errors import NetworkError, TooManyRequestsError
from binance_common.models import ApiResponse, RateLimit
from binance_sdk_spot.rest_api import models as m

from bot import cli
from bot.core.config import Environment
from bot.exchange.binance import (
    READ_ONLY_CALLS,
    BinanceReadOnly,
    Credentials,
    ExchangeUnavailable,
    MissingCredentials,
    build_sdk_client,
    load_credentials,
)
from bot.exchange.market import ExchangeDataError
from tests.conftest import exchange_info_symbol
from tests.unit.test_exchange_market import kline_row

T0 = 1_700_000_000_000


def payloads():
    return {
        "time": (m.TimeResponse, {"serverTime": T0 + 40}),
        "exchange_info": (m.ExchangeInfoResponse, {
            "timezone": "UTC", "serverTime": T0, "exchangeFilters": [],
            "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000}],
            "symbols": [exchange_info_symbol("BTCUSDT"), exchange_info_symbol("ETHUSDT", step="0.0001")],
        }),
        "my_filters": (m.MyFiltersResponse, {
            "exchangeFilters": [], "symbolFilters": [],
            "assetFilters": [{"filterType": "MAX_ASSET", "asset": "BTC", "limit": "0.5"}],
        }),
        "ticker_book_ticker": (m.TickerBookTickerResponse, [
            {"symbol": "BTCUSDT", "bidPrice": "79990.00", "bidQty": "1", "askPrice": "80000.00", "askQty": "2"},
            {"symbol": "ETHUSDT", "bidPrice": "3999.00", "bidQty": "10", "askPrice": "4000.00", "askQty": "9"},
        ]),
        "klines": (None, [kline_row(T0 - 7_200_000), kline_row(T0 - 3_600_000), kline_row(T0)]),
        "get_account": (m.GetAccountResponse, {
            "makerCommission": 10, "canTrade": True, "canWithdraw": False, "canDeposit": True, "brokered": False,
            "requireSelfTradePrevention": False, "preventSor": False, "updateTime": T0, "accountType": "SPOT",
            "balances": [{"asset": "USDT", "free": "10000.00", "locked": "0.00"}], "permissions": ["SPOT"], "uid": 1,
        }),
        "account_commission": (m.AccountCommissionResponse, {
            "symbol": "BTCUSDT",
            "standardCommission": {"maker": "0.001", "taker": "0.001", "buyer": "0", "seller": "0"},
            "specialCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
            "taxCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
            "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.75"},
        }),
    }


class FakeRest:
    """Stands in for binance_sdk_spot SpotRestAPI. Any method not in the payload table explodes."""

    def __init__(self, overrides=None, weight=10, error=None):
        self.data = {**payloads(), **(overrides or {})}
        self.calls = []
        self.weight = weight
        self.error = error

    def __getattr__(self, name):
        if name not in self.data:
            raise AssertionError(f"adapter called unexpected SDK method {name}")

        def method(*args, **kwargs):
            self.calls.append((name, kwargs))
            if self.error:
                raise self.error
            model, raw = self.data[name]
            fn = (lambda: model.from_dict(raw)) if model else (lambda: raw)
            limits = [RateLimit(rateLimitType="REQUEST_WEIGHT", interval="MINUTE", intervalNum=1, count=self.weight,
                                retryAfter=None)]
            return ApiResponse(fn, 200, {}, limits)
        return method


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def adapter(rest=None, signed=True, clock=None):
    return BinanceReadOnly(rest or FakeRest(), signed=signed, clock=clock or Clock())


def test_symbol_rules_include_account_max_asset_when_signed():
    rest = FakeRest()
    rules = adapter(rest).symbol_rules(["ETHUSDT", "BTCUSDT"])
    assert set(rules) == {"BTCUSDT", "ETHUSDT"}
    assert rules["BTCUSDT"].tick_size == Dec("0.01") and rules["ETHUSDT"].step_size == Dec("0.0001")
    assert rules["BTCUSDT"].max_asset_qty == Dec("0.5")
    assert [c[0] for c in rest.calls].count("my_filters") == 2


def test_symbol_rules_public_only_skips_my_filters():
    rest = FakeRest()
    rules = adapter(rest, signed=False).symbol_rules(["BTCUSDT"])
    assert rules["BTCUSDT"].max_asset_qty is None
    assert all(c[0] != "my_filters" for c in rest.calls)


def test_missing_symbol_fails_closed():
    with pytest.raises(ExchangeDataError, match="SOLUSDT"):
        adapter().symbol_rules(["BTCUSDT", "SOLUSDT"])


def test_non_usdt_or_non_spot_symbol_refused():
    ei = payloads()["exchange_info"][1]
    bad_quote = {**ei, "symbols": [{**exchange_info_symbol("BTCUSDT"), "quoteAsset": "FDUSD"}]}
    with pytest.raises(ExchangeDataError, match="USDT"):
        adapter(FakeRest({"exchange_info": (m.ExchangeInfoResponse, bad_quote)})).symbol_rules(["BTCUSDT"])
    no_spot = {**ei, "symbols": [{**exchange_info_symbol("BTCUSDT"), "isSpotTradingAllowed": False}]}
    with pytest.raises(ExchangeDataError, match="Spot"):
        adapter(FakeRest({"exchange_info": (m.ExchangeInfoResponse, no_spot)})).symbol_rules(["BTCUSDT"])


def test_book_tickers_and_missing_symbol():
    books = adapter().book_tickers(["BTCUSDT", "ETHUSDT"])
    assert books["ETHUSDT"].ask == Dec("4000.00") and books["BTCUSDT"].received_ms == T0
    with pytest.raises(ExchangeDataError):
        adapter().book_tickers(["BTCUSDT", "XRPUSDT"])


def test_klines_drop_the_open_candle_by_default():
    clock = Clock(T0 + 600_000)
    ks = adapter(clock=clock).klines("BTCUSDT", "1h", limit=3)
    assert [k.open_time for k in ks] == [T0 - 7_200_000, T0 - 3_600_000]
    assert len(adapter(clock=clock).klines("BTCUSDT", "1h", limit=3, closed_only=False)) == 3


@pytest.mark.parametrize("kwargs", [{"interval": "2d"}, {"interval": "1h", "limit": 0}, {"interval": "1h", "limit": 1001}])
def test_klines_argument_checks(kwargs):
    with pytest.raises(ValueError):
        adapter().klines("BTCUSDT", **kwargs)


def test_clock_skew():
    clock = Clock(T0)
    assert adapter(clock=clock).clock_skew_ms() == 40


def test_account_and_commission():
    ex = adapter()
    acct = ex.account()
    assert acct.balances[0].asset == "USDT" and acct.balances[0].free == Dec("10000.00")
    c = ex.commission("BTCUSDT")
    assert c.taker == Dec("0.001")


def test_commission_for_wrong_symbol_rejected():
    raw = {**payloads()["account_commission"][1], "symbol": "ETHUSDT"}
    with pytest.raises(ExchangeDataError):
        adapter(FakeRest({"account_commission": (m.AccountCommissionResponse, raw)})).commission("BTCUSDT")


def test_signed_calls_need_a_key():
    ex = adapter(signed=False)
    with pytest.raises(MissingCredentials):
        ex.account()
    with pytest.raises(MissingCredentials):
        ex.commission("BTCUSDT")


@pytest.mark.parametrize("name", ["new_order", "delete_order", "order_list_opoco", "order_oco", "send_request",
                                  "send_signed_request", "delete_open_orders", "order_cancel_replace"])
def test_order_endpoints_cannot_be_called(name):
    with pytest.raises(PermissionError):
        adapter()._call(name)


def test_sdk_errors_fail_closed():
    with pytest.raises(ExchangeUnavailable):
        adapter(FakeRest(error=NetworkError("down"))).book_tickers(["BTCUSDT"])


def test_429_pauses_calls_until_retry_after():
    clock = Clock()
    rest = FakeRest(error=TooManyRequestsError("slow down", -1003, retry_after=30))
    ex = adapter(rest, clock=clock)
    with pytest.raises(ExchangeUnavailable):
        ex.server_time_ms()
    rest.error = None
    clock.t += 29_000
    with pytest.raises(ExchangeUnavailable, match="guard"):
        ex.server_time_ms()
    assert len(rest.calls) == 1                 # the guard did not hit the exchange
    clock.t += 1_000
    assert ex.server_time_ms() == T0 + 40


def test_high_used_weight_pauses_until_next_minute():
    clock = Clock(T0)                            # T0 is 20 s into a minute
    rest = FakeRest(weight=4900)                 # above 80% of 6000
    ex = adapter(rest, clock=clock)
    ex.server_time_ms()
    assert ex.used_weight_1m == 4900
    with pytest.raises(ExchangeUnavailable, match="guard"):
        ex.server_time_ms()
    clock.t = (T0 // 60_000 + 1) * 60_000
    rest.weight = 10
    ex.server_time_ms()


def test_malformed_response_is_a_data_error():
    with pytest.raises(ExchangeDataError):
        adapter(FakeRest({"time": (None, {"oops": 1})})).server_time_ms()


# ------------------------------------------------------------------ credentials
def test_no_key_means_public_only(tmp_path):
    assert load_credentials({}, repo_root=tmp_path) is None


def test_credentials_never_show_the_key(tmp_path):
    key = tmp_path / "keys" / "ed25519.pem"
    key.parent.mkdir()
    key.write_text("not a real key")
    creds = load_credentials({"BINANCE_API_KEY": "SECRET-API-KEY", "BINANCE_PRIVATE_KEY_PATH": str(key)},
                             repo_root=tmp_path / "repo")
    assert creds.api_key == "SECRET-API-KEY"
    assert "SECRET" not in repr(creds) and "SECRET" not in str(creds)


def test_key_inside_repo_refused(tmp_path):
    key = tmp_path / "secret.pem"
    key.write_text("x")
    with pytest.raises(MissingCredentials, match="inside the repository"):
        load_credentials({"BINANCE_API_KEY": "k", "BINANCE_PRIVATE_KEY_PATH": str(key)}, repo_root=tmp_path)


def test_key_without_private_key_path_refused(tmp_path):
    with pytest.raises(MissingCredentials):
        load_credentials({"BINANCE_API_KEY": "k"}, repo_root=tmp_path)
    with pytest.raises(MissingCredentials, match="not found"):
        load_credentials({"BINANCE_API_KEY": "k", "BINANCE_PRIVATE_KEY_PATH": str(tmp_path / "nope.pem")},
                         repo_root=tmp_path / "repo")


def test_sdk_client_uses_the_environment_url():
    rest = build_sdk_client(Environment.TESTNET, None)
    assert rest.configuration.base_path == "https://testnet.binance.vision"
    assert all(hasattr(rest, name) for name in READ_ONLY_CALLS)


# ------------------------------------------------------------------ CLI
def test_cli_exchange_check(monkeypatch, capsys):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_connect", lambda env, creds: adapter(signed=False))
    assert cli.main(["exchange-check", "--env", "testnet"]) == 0
    out = capsys.readouterr().out
    assert "Clock skew: 40 ms (OK" in out and "BTCUSDT: status TRADING" in out and "no orders were sent" in out


def test_cli_exchange_check_fails_closed(monkeypatch, capsys):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_connect", lambda env, creds: adapter(FakeRest(error=NetworkError("down")), signed=False))
    assert cli.main(["exchange-check"]) == 3
    assert "blocked" in capsys.readouterr().err


def test_cli_reports_halted_symbol(monkeypatch, capsys):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    ei = {**payloads()["exchange_info"][1], "symbols": [exchange_info_symbol("BTCUSDT", status="BREAK")]}
    rest = FakeRest({"exchange_info": (m.ExchangeInfoResponse, ei)})
    monkeypatch.setattr(cli, "_connect", lambda env, creds: adapter(rest, signed=False))
    assert cli.main(["exchange-check", "--symbols", "BTCUSDT"]) == 1
    assert "not trading" in capsys.readouterr().out


def test_cli_signed_check_prints_fees_not_key(monkeypatch, capsys, tmp_path):
    key = tmp_path / "k.pem"
    key.write_text("x")
    monkeypatch.setenv("BINANCE_API_KEY", "SECRET-API-KEY")
    monkeypatch.setenv("BINANCE_PRIVATE_KEY_PATH", str(key))
    monkeypatch.setattr(cli, "_connect", lambda env, creds: adapter(signed=True))
    assert cli.main(["exchange-check", "--symbols", "BTCUSDT"]) == 0
    out = capsys.readouterr().out
    assert "taker 0.001" in out and "USDT: free 10000.00" in out and "SECRET" not in out
