"""Module 5a: the testnet trading adapter. Never two orders for one idea, pre-send checks, allow-listed
endpoints, testnet only, and `bot check-trading` places nothing."""
import argparse
import inspect
from decimal import Decimal as Dec

import pytest

from bot import cli
from bot.core.ids import client_order_id, new_intent_id
from bot.exchange import trading
from bot.exchange.binance import ExchangeError
from bot.exchange.credentials import CredentialsError, load_credentials
from bot.exchange.filters import parse_symbol
from bot.exchange.trading import (AccountInfo, BinanceTrader, OrderRejected, OrderRequest, OrderStatusUnknown,
                                  check_trading_account)
from tests.conftest import exchange_info_symbol
from tests.fakes import FakeMarket

RULES = parse_symbol(exchange_info_symbol("BTCUSDT"))     # tick 0.01, step 0.00001, min notional 5
ORDER = ("POST", "/api/v3/order")
QUERY = ("GET", "/api/v3/order")


def sdk_error(name, code=None, msg="error"):
    """An exception shaped like binance_common.errors.* (class name, status_code, error_message)."""
    cls = type(name, (Exception,), {})
    e = cls(msg)
    e.status_code, e.error_message = code, msg
    return e


class FakeClient:
    def __init__(self):
        self.responses, self.calls = {}, []

    def script(self, method_path, *results):
        self.responses[method_path] = list(results)
        return self

    def send(self, method, endpoint, params):
        self.calls.append((method, endpoint, dict(params)))
        q = self.responses[(method, endpoint)]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        return r

    def count(self, method_path):
        return sum(1 for m, e, _ in self.calls if (m, e) == method_path)


def filled(cid, qty="0.00012", status="FILLED"):
    """Shape from the Spot API docs (New order, FULL response)."""
    return {"symbol": "BTCUSDT", "orderId": 28, "orderListId": -1, "clientOrderId": cid, "transactTime": 1,
            "price": "80000.00", "origQty": qty, "executedQty": qty if status == "FILLED" else "0",
            "cummulativeQuoteQty": "9.60000000" if status == "FILLED" else "0", "status": status,
            "timeInForce": "IOC", "type": "LIMIT", "side": "BUY",
            "fills": [{"price": "80000.00", "qty": qty, "commission": "0.00000012", "commissionAsset": "BTC",
                       "tradeId": 56}] if status == "FILLED" else []}


def buy(qty="0.00012", price="80000.00", cid=None):
    return OrderRequest("BTCUSDT", "BUY", "LIMIT", Dec(qty), cid or client_order_id(new_intent_id(), "entry"),
                        price=Dec(price), time_in_force="IOC")


def trader(client):
    return BinanceTrader(environment="testnet", client=client, _sleep=lambda s: None)


# ---------------------------------------------------------------- testnet only, allow-listed endpoints
@pytest.mark.parametrize("env", ["live", "paper", "sim"])
def test_orders_only_on_the_testnet(env):
    with pytest.raises(ExchangeError, match="only on the Binance testnet"):
        BinanceTrader(environment=env, client=FakeClient())
    with pytest.raises(ExchangeError, match="testnet trading key"):
        BinanceTrader(environment="testnet")


def test_only_allow_listed_endpoints_and_none_that_moves_money():
    t = trader(FakeClient())
    for method, path in (("POST", "/sapi/v1/capital/withdraw/apply"), ("POST", "/api/v3/orderList/oco"),
                         ("GET", "/api/v3/allOrders"), ("POST", "/sapi/v1/asset/transfer")):
        with pytest.raises(ExchangeError, match="may call"):
            t._call(method, path)
    for _, path in trading.TRADE_ENDPOINTS:
        assert path.startswith("/api/v3/") and not any(w in path.lower() for w in ("withdraw", "transfer", "margin"))


def test_the_sdk_never_retries_an_order_itself():
    assert "retries=0" in inspect.getsource(trading._sdk_trade_client)


# ---------------------------------------------------------------- placing
def test_a_limit_order_is_sent_as_binance_expects():
    c = FakeClient().script(ORDER, filled("x"))
    req = buy(qty="0.00012000", price="80000.00")
    c.responses[ORDER] = [filled(req.client_order_id)]
    s = trader(c).place(req, RULES)
    (method, path, params), = c.calls
    assert params == {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.00012", "price": "80000",
                      "timeInForce": "IOC", "newClientOrderId": req.client_order_id, "newOrderRespType": "FULL"}
    assert s.status == "FILLED" and s.executed_qty == Dec("0.00012") and s.final
    assert s.fills[0].commission_asset == "BTC" and s.quote_qty == Dec("9.6")


@pytest.mark.parametrize("bad, why", [
    (OrderRequest("BTCUSDT", "BUY", "MARKET", Dec("0.001"), client_order_id(new_intent_id(), "entry")), "exits only"),
    (buy(qty="0.000123"), "stepSize"),
    (buy(qty="0.00005"), "below minimum"),                       # 4 USDT < 5 USDT minimum
    (buy(price="80000.005"), "tickSize"),
    (OrderRequest("BTCUSDT", "BUY", "LIMIT", Dec("0.001"), client_order_id(new_intent_id(), "entry"),
                  price=Dec("80000")), "needs a price and GTC"),
    (buy(cid="has spaces!"), "not a valid Binance ID"),
    (OrderRequest("ETHUSDT", "BUY", "LIMIT", Dec("0.001"), client_order_id(new_intent_id(), "entry"),
                  price=Dec("2500"), time_in_force="IOC"), "rules are for"),
])
def test_bad_orders_are_never_sent(bad, why):
    c = FakeClient().script(ORDER, filled("x"))
    with pytest.raises(OrderRejected, match=why):
        trader(c).place(bad, RULES)
    assert c.calls == []


def test_a_market_sell_exit_is_allowed():
    req = OrderRequest("BTCUSDT", "SELL", "MARKET", Dec("0.001"), client_order_id(new_intent_id(), "exit"))
    c = FakeClient().script(ORDER, {**filled(req.client_order_id), "type": "MARKET", "side": "SELL"})
    trader(c).place(req, RULES)
    assert c.calls[0][2]["type"] == "MARKET" and "price" not in c.calls[0][2]


# ---------------------------------------------------------------- never two orders for one idea
@pytest.mark.parametrize("failure", [sdk_error("NetworkError", None, "read timed out"),
                                     sdk_error("ServerError", 503, "Server error: 503"),
                                     sdk_error("BadRequestError", -1007, "Timeout waiting for response"),
                                     sdk_error("BadRequestError", -1006, "Unexpected response"),
                                     RuntimeError("Request failed after 0 retries.")])
def test_an_unknown_outcome_is_looked_up_never_resent(failure):
    req = buy()
    c = FakeClient().script(ORDER, failure).script(QUERY, filled(req.client_order_id))
    s = trader(c).place(req, RULES)
    assert s.status == "FILLED" and c.count(ORDER) == 1 and c.count(QUERY) == 1
    assert c.calls[1][2] == {"symbol": "BTCUSDT", "origClientOrderId": req.client_order_id}


def test_still_unknown_after_lookups_blocks_and_sends_nothing_more():
    req = buy()
    c = FakeClient().script(ORDER, sdk_error("NetworkError")).script(
        QUERY, sdk_error("BadRequestError", -2013, "Order does not exist."))
    with pytest.raises(OrderStatusUnknown, match="blocked until reconciliation") as e:
        trader(c).place(req, RULES)
    assert e.value.client_order_id == req.client_order_id
    assert c.count(ORDER) == 1 and c.count(QUERY) == 3


def test_a_lookup_that_appears_late_is_found():
    req = buy()
    c = FakeClient().script(ORDER, sdk_error("NetworkError")).script(
        QUERY, sdk_error("BadRequestError", -2013, "Order does not exist."), sdk_error("NetworkError"),
        filled(req.client_order_id))
    assert trader(c).place(req, RULES).status == "FILLED" and c.count(ORDER) == 1


def test_a_duplicate_id_means_it_was_sent_before_find_it():
    req = buy()
    c = FakeClient().script(ORDER, sdk_error("BadRequestError", -2010, "Duplicate order sent.")).script(
        QUERY, filled(req.client_order_id, status="NEW"))
    assert trader(c).place(req, RULES).status == "NEW" and c.count(ORDER) == 1


@pytest.mark.parametrize("refusal", [sdk_error("BadRequestError", -2010, "Account has insufficient balance."),
                                     sdk_error("TooManyRequestsError", -1003, "Too many requests"),
                                     sdk_error("UnauthorizedError", -2015, "Invalid API-key, IP, or permissions")])
def test_a_definite_refusal_is_reported_and_not_looked_up(refusal):
    c = FakeClient().script(ORDER, refusal)
    with pytest.raises(OrderRejected) as e:
        trader(c).place(buy(), RULES)
    assert e.value.code == refusal.status_code and c.count(QUERY) == 0 and c.count(ORDER) == 1


# ---------------------------------------------------------------- query, cancel, account
def test_query_cancel_and_open_orders():
    cid = client_order_id(new_intent_id(), "entry")
    c = FakeClient().script(QUERY, sdk_error("BadRequestError", -2013, "Order does not exist."))
    assert trader(c).query("BTCUSDT", cid) is None
    c = FakeClient().script(("DELETE", "/api/v3/order"), sdk_error("BadRequestError", -2011, "Unknown order sent.")
                            ).script(QUERY, filled(cid))
    assert trader(c).cancel("BTCUSDT", cid).status == "FILLED"            # it filled first: the real state
    c = FakeClient().script(("DELETE", "/api/v3/order"), sdk_error("NetworkError")).script(QUERY, filled(cid, status="CANCELED"))
    assert trader(c).cancel("BTCUSDT", cid).status == "CANCELED"
    c = FakeClient().script(("GET", "/api/v3/openOrders"), [filled(cid, status="NEW")])
    (o,), = [trader(c).open_orders("BTCUSDT")]
    assert o.status == "NEW" and not o.final


def test_account_and_the_key_check():
    c = FakeClient().script(("GET", "/api/v3/account"), {
        "canTrade": True, "canWithdraw": True, "accountType": "SPOT", "permissions": ["SPOT"],
        "balances": [{"asset": "USDT", "free": "10000.0", "locked": "0"}, {"asset": "BTC", "free": "0", "locked": "0"}]})
    info = trader(c).account()
    assert info.balances[0].total == Dec(10000) and len(info.balances) == 1
    ok = check_trading_account(info, "testnet")
    assert ok.safe and "cannot be read here" in ok.notes[0]
    assert not check_trading_account(info, "live").safe                    # live: a key that can withdraw is a stop
    for bad in (AccountInfo(False, False, "SPOT", ("SPOT",), ()), AccountInfo(True, False, "MARGIN", (), ()),
                AccountInfo(True, False, "SPOT", ("SPOT", "MARGIN"), ())):
        assert not check_trading_account(bad, "testnet").safe


def test_decimals_are_sent_without_exponents():
    assert trading._fmt(Dec("0.00010")) == "0.0001" and trading._fmt(Dec("1E+1")) == "10"
    assert trading._fmt(Dec("80000.00")) == "80000"


# ---------------------------------------------------------------- credentials: the testnet key is separate
def test_the_testnet_key_has_its_own_settings(tmp_path):
    pem = tmp_path / "k.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    pem.chmod(0o600)
    live_only = {"BINANCE_API_KEY": "A" * 64, "BINANCE_PRIVATE_KEY_PATH": str(pem), "BOT_ENV_FILE": str(tmp_path / "none")}
    with pytest.raises(CredentialsError, match="BINANCE_TESTNET_API_KEY"):
        load_credentials(environ=live_only, prefix="BINANCE_TESTNET")    # the live key is never used for orders
    both = {**live_only, "BINANCE_TESTNET_API_KEY": "B" * 64, "BINANCE_TESTNET_PRIVATE_KEY_PATH": str(pem)}
    assert load_credentials(environ=both, prefix="BINANCE_TESTNET").api_key == "B" * 64
    assert load_credentials(environ=both).api_key == "A" * 64


# ---------------------------------------------------------------- bot check-trading
def run_check(client, capsys, creds=object()):
    def load():
        if creds is None:
            raise CredentialsError("BINANCE_TESTNET_API_KEY is not set")
        return creds
    code = cli._check_trading(argparse.Namespace(symbol="BTCUSDT"),
                              make_trader=lambda **k: BinanceTrader(environment="testnet", client=client),
                              make_reader=lambda **k: FakeMarket(), load_creds=load)
    return code, capsys.readouterr().out


def account(**over):
    return {"canTrade": True, "canWithdraw": True, "accountType": "SPOT", "permissions": ["SPOT"],
            "balances": [{"asset": "USDT", "free": "10000", "locked": "0"}], **over}


def test_check_trading_validates_a_test_order_and_places_nothing(capsys):
    c = FakeClient().script(("GET", "/api/v3/account"), account()).script(("POST", "/api/v3/order/test"), {}
                                                                          ).script(("GET", "/api/v3/openOrders"), [])
    code, out = run_check(c, capsys)
    assert code == 0 and "TESTNET TRADING KEY WORKS. Nothing was bought or sold." in out
    assert c.count(ORDER) == 0 and c.count(("POST", "/api/v3/order/test")) == 1
    test = [p for m, e, p in c.calls if e == "/api/v3/order/test"][0]
    assert Dec(test["price"]) <= Dec("79990") * Dec("0.9") and Dec(test["price"]) * Dec(test["quantity"]) >= 5


def test_check_trading_stops_on_an_unsafe_key_a_refusal_or_no_key(capsys):
    c = FakeClient().script(("GET", "/api/v3/account"), account(canTrade=False))
    code, out = run_check(c, capsys)
    assert code == 3 and "trading is not enabled" in out
    c = FakeClient().script(("GET", "/api/v3/account"), account()).script(
        ("POST", "/api/v3/order/test"), sdk_error("BadRequestError", -1013, "Filter failure: NOTIONAL"))
    code, out = run_check(c, capsys)
    assert code == 4 and "REFUSED" in out and c.count(ORDER) == 0
    code, out = run_check(FakeClient(), capsys, creds=None)
    assert code == 2 and "NOT READY" in out
