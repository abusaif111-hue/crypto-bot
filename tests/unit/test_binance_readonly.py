"""Module 2: read-only Binance adapter. Public fixtures are real Binance responses
(tests/fixtures/binance, captured 22 Oct 2026); signed responses follow Binance's
documented shapes. No test touches the network."""
import json
import subprocess
from base64 import b64decode
from decimal import Decimal as Dec
from pathlib import Path
from urllib.parse import urlencode

import pytest

from bot.core.config import AppConfig
from bot.exchange.binance import (
    READ_ENDPOINTS, BinanceReadOnly, ExchangeError, KeyPermissions, _sdk_client,
)
from bot.exchange.credentials import Credentials
from bot.portfolio.accounting import Balance
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, MarketSnapshot, Mode, TradeIntent

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "binance"
SAFE_PERMS = {"ipRestrict": True, "createTime": 1, "enableReading": True, "enableWithdrawals": False,
              "enableInternalTransfer": False, "enableMargin": False, "enableFutures": False,
              "permitsUniversalTransfer": False, "enableVanillaOptions": False, "enableFixApiTrade": False,
              "enableFixReadOnly": True, "enableSpotAndMarginTrading": False, "enablePortfolioMarginTrading": False}


def fx(name):
    return json.loads((FIX / name).read_text())


class Resp:
    def __init__(self, data, headers=None):
        self._d, self.headers = data, headers or {}

    def data(self):
        return self._d


class FakeClient:
    def __init__(self, routes, headers=None, fail=None):
        self.routes, self.calls, self.headers, self.fail = routes, [], headers or {}, fail or {}

    def _resp(self, endpoint, method, params, signed):
        assert method == "GET"
        self.calls.append((endpoint, signed, dict(params or {})))
        if endpoint in self.fail:
            raise self.fail[endpoint]
        r = self.routes[endpoint]
        return Resp(r(params) if callable(r) else r, self.headers)

    def send_request(self, endpoint, method, query_params=None):
        return self._resp(endpoint, method, query_params, False)

    def send_signed_request(self, endpoint, method, query_params=None):
        return self._resp(endpoint, method, query_params, True)


def book_route(params):
    syms = json.loads(params["symbols"])
    known = {d["symbol"]: d for d in fx("book_ticker.json")}
    if any(s not in known for s in syms):
        raise RuntimeError("Invalid symbol.")
    return [known[s] for s in syms]


PUBLIC = {
    "/api/v3/time": fx("time.json"),
    "/api/v3/exchangeInfo": fx("exchange_info.json"),
    "/api/v3/ticker/bookTicker": book_route,
    "/api/v3/ticker/24hr": fx("ticker_24hr.json"),
    "/api/v3/depth": fx("depth_btc.json"),
    "/api/v3/klines": fx("klines_btc_1d.json"),
    "/api/v3/avgPrice": {"mins": 5, "price": "82010.11000000", "closeTime": 1792667463000},  # documented shape
}
SIGNED = {
    "/sapi/v1/account/apiRestrictions": SAFE_PERMS,
    "/api/v3/myFilters": {"exchangeFilters": [], "symbolFilters": [], "assetFilters": []},
    "/api/v3/account/commission": {"symbol": "BTCUSDT",
                                   "standardCommission": {"maker": "0.00100000", "taker": "0.00100000", "buyer": "0", "seller": "0"},
                                   "taxCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
                                   "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.25"}},
    "/api/v3/account": {"balances": [
        {"asset": "USDT", "free": "950.50000000", "locked": "49.50000000"},
        {"asset": "BTC", "free": "0.01000000", "locked": "0.00000000"},
        {"asset": "LDBTC", "free": "0.00100000", "locked": "0.00000000"},
        {"asset": "ETH", "free": "0.00000000", "locked": "0.00000000"}]},
}


def make(signed=True, **kw):
    creds = Credentials(api_key="A" * 64, private_key_path=Path("/nonexistent")) if signed else None
    return BinanceReadOnly(environment="live", credentials=creds, public_client=FakeClient(PUBLIC, **kw),
                           signed_client=FakeClient(SIGNED) if signed else None, clock=lambda: 1792580000.0)


# ---------------------------------------------------------------- public data
def test_real_exchange_info_parses_and_matches_risk_engine():
    ex = make(signed=False)
    rules = ex.symbol_rules(["BTCUSDT", "ETHUSDT"])
    btc = rules["BTCUSDT"]
    assert btc.is_trading and btc.oco_allowed and btc.opo_allowed
    assert btc.tick_size == Dec("0.01") and btc.step_size == Dec("0.00001") and btc.min_notional == Dec("5")
    # The real BTC rules work end to end with the module-1 risk engine
    intent = TradeIntent("BTCUSDT", "BUY", Dec("82000"), Dec("80000"), Dec("88000"), "t", 48, "test")
    state = AccountState(Mode.RUNNING, Dec("10000"), Dec("10000"), Dec("10000"), Dec("10000"), Dec("0"), Dec("0"), Dec("0"))
    mkt = MarketSnapshot(Dec("81999.99"), Dec("82000"), Dec("1e9"), Dec("3e6"), Dec("1"), 10, Dec("82000"))
    d = evaluate(intent, state, mkt, btc, AppConfig(), "majors")
    assert d.accepted, d.reason


def test_books_volumes_depth_klines():
    ex = make(signed=False)
    b = ex.books(["BTCUSDT", "ETHUSDT"])["BTCUSDT"]
    assert b.ask >= b.bid > 0 and b.spread_pct < Dec("0.01")
    assert ex.quote_volume_24h(["BTCUSDT"])["BTCUSDT"] > Dec("1e8")
    depth = ex.ask_depth_quote("BTCUSDT")
    asks = fx("depth_btc.json")["asks"]
    assert depth == sum(Dec(p) * Dec(q) for p, q in asks if Dec(p) <= Dec(asks[0][0]) * Dec("1.005"))
    k = ex.klines("BTCUSDT", "1d", limit=3)
    assert len(k) == 3 and all(c.high >= c.low for c in k)
    assert [c.closed for c in k] == [int(r[6]) < 1792580000000 for r in fx("klines_btc_1d.json")]


def test_clock_skew_discards_warmup_and_uses_fastest_round_trip():
    # Server clock = local clock exactly. Warm-up call has no timing; sample 1 is slow and
    # lopsided (server stamped late in a 1.2 s round trip), sample 2 is fast and fair.
    server_times = iter([0, 101_150, 105_010])
    ticks = iter([100.0, 101.2, 105.0, 105.02])
    ex = BinanceReadOnly(environment="live",
                         public_client=FakeClient({"/api/v3/time": lambda p: {"serverTime": next(server_times)}}),
                         clock=lambda: next(ticks))
    assert ex.clock_skew_ms(samples=2) == 0      # the slow sample alone would have said -550 ms


# ---------------------------------------------------------------- safety of the adapter
def test_only_listed_get_endpoints_can_be_called():
    ex = make()
    with pytest.raises(ExchangeError, match="not a read-only endpoint"):
        ex._get("/api/v3/order", {"symbol": "BTCUSDT"})
    assert not any("order" in e.lower() for e in READ_ENDPOINTS)


def test_signed_call_without_key_is_refused():
    with pytest.raises(ExchangeError, match="needs the API key"):
        make(signed=False).balances()


def test_errors_do_not_leak_request_details():
    class Boom(Exception):
        error_message = "Invalid API-key, IP, or permissions for action."
    ex = make()
    ex.signed_client.fail = {"/api/v3/account": Boom()}
    with pytest.raises(ExchangeError) as e:
        ex.balances()
    assert "signature" not in str(e.value) and "Invalid API-key" in str(e.value)


def test_backs_off_when_request_weight_is_high():
    slept = []
    ex = make(signed=False, headers={"X-MBX-USED-WEIGHT-1M": "4500"})
    ex._sleep = slept.append
    ex.server_time_ms()
    assert ex.used_weight == 4500
    ex.server_time_ms()
    assert slept == [5]


# ---------------------------------------------------------------- account data
def test_permissions_safe_key():
    p = make().key_permissions()
    assert p.safe and p.warnings == ()


@pytest.mark.parametrize("change,expected", [
    ({"enableSpotAndMarginTrading": True}, "enableSpotAndMarginTrading is ON"),
    ({"enableWithdrawals": True}, "enableWithdrawals is ON"),
    ({"enableFutures": True}, "enableFutures is ON"),
    ({"ipRestrict": False}, "not restricted to trusted IPs"),
    ({"enableReading": False}, "Enable Reading is off"),
])
def test_permissions_hard_stops(change, expected):
    p = KeyPermissions.classify({**SAFE_PERMS, **change})
    assert not p.safe and any(expected in x for x in p.problems)


def test_permissions_warnings_and_unknown_fields():
    p = KeyPermissions.classify({**SAFE_PERMS, "permitsUniversalTransfer": True, "enableSomethingNew": True})
    assert p.safe and len(p.warnings) == 2


def test_balances_fees_and_valuation():
    ex = make()
    bals = ex.balances()
    assert [b.asset for b in bals] == ["USDT", "BTC", "LDBTC"]       # zero ETH dropped
    assert ex.taker_fee("BTCUSDT") == Dec("0.001")
    equity, unvalued = ex.value_balances(bals)
    btc_bid = Dec(next(d for d in fx("book_ticker.json") if d["symbol"] == "BTCUSDT")["bidPrice"])
    assert equity == Dec("1000") + Dec("0.01") * btc_bid
    assert unvalued == ["LDBTC"]                                        # never silently counted as zero


# ---------------------------------------------------------------- real SDK signing
def test_sdk_signs_with_openssl_ed25519_key(tmp_path):
    """Same key format as scripts/make_binance_key.sh; the request leaves with a valid Ed25519 signature."""
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa

    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    key.chmod(0o600)
    client = _sdk_client("https://api.binance.com", Credentials(api_key="B" * 64, private_key_path=key))

    sent = {}

    class Http:
        def request(self, method, url, params=None, headers=None, data=None, **kw):
            sent.update(method=method, url=url, params=params, headers=headers)

            class R:
                status_code, headers, text = 200, {"Content-Type": "application/json"}, '{"balances": []}'
            return R()

    client._sdk._session = Http()
    out = client.send_signed_request("/api/v3/account", "GET", {"omitZeroBalances": "true"}).data()
    assert out == {"balances": []}
    assert sent["method"] == "GET" and sent["url"].endswith("/api/v3/account")
    assert sent["headers"]["X-MBX-APIKEY"] == "B" * 64
    params = dict(p.split("=", 1) for p in sent["params"].split("&"))
    from urllib.parse import unquote
    sig = b64decode(unquote(params.pop("signature")))
    message = urlencode(params).encode()
    pub = ECC.import_key(key.read_text()).public_key()
    eddsa.new(pub, "rfc8032").verify(message, sig)            # raises if the signature is wrong
    with pytest.raises(ExchangeError):
        client._call("/api/v3/order", "POST", {}, signed=True)  # the wrapper itself refuses non-GET


def test_avg_price_is_a_public_read():
    client = FakeClient(PUBLIC)
    ex = BinanceReadOnly(public_client=client, _sleep=lambda s: None)
    assert ex.avg_price("BTCUSDT") == Dec("82010.11")
    assert client.calls[-1] == ("/api/v3/avgPrice", False, {"symbol": "BTCUSDT"})
    assert READ_ENDPOINTS["/api/v3/avgPrice"] is False
