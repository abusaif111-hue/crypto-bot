"""Module 5b: protected entries. Risk engine first, one OPOCO list (BUY fill-or-kill, then stop and
take-profit), the protection confirmed after the fill, an emergency exit when it is not, and testnet
round trips recorded toward the 5-clean-round-trips gate."""
import argparse
import ast
import io
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

import bot.execution.entry as entry_mod
from bot import cli
from bot.core.ids import client_order_id, new_intent_id
from bot.exchange.filters import parse_symbol
from bot.exchange.trading import BinanceTrader, OrderStatusUnknown
from bot.execution import entry, roundtrip
from bot.risk.models import Mode
from tests.conftest import exchange_info_symbol
from tests.fakes import FakeMarket
from tests.unit.test_trading_adapter import FakeClient, sdk_error

OPOCO = ("POST", "/api/v3/orderList/opoco")
QUERY = ("GET", "/api/v3/order")
CANCEL_LIST = ("DELETE", "/api/v3/orderList")
ORDER = ("POST", "/api/v3/order")
SRC = Path(__file__).resolve().parents[2] / "src" / "bot"


def report(cid, status, side="BUY", type_="LIMIT", qty="0.06233", price="80000", stop=None, executed=None,
           fills=(), quote="0"):
    d = {"symbol": "BTCUSDT", "orderId": abs(hash(cid)) % 10**6, "orderListId": 7, "clientOrderId": cid,
         "price": price, "origQty": qty, "executedQty": executed if executed is not None else
         (qty if status == "FILLED" else "0"), "cummulativeQuoteQty": quote, "status": status,
         "timeInForce": "FOK" if side == "BUY" else "GTC", "type": type_, "side": side, "fills": list(fills)}
    if stop:
        d["stopPrice"] = stop
    return d


class Exchange:
    """A scripted Binance: the OPOCO reply, then what each protective order looks like when queried."""

    def __init__(self, intent_id, entry_status="FILLED", stop_status="NEW", tp_status="NEW", fee_in_coin="0",
                 stop_type="STOP_LOSS", stop_price=None, prot_qty=None, opoco_error=None, sell_error=None):
        self.ids = {k: client_order_id(intent_id, *k) for k in (("list",), ("entry",), ("protect", 0),
                                                                 ("protect", 1), ("exit",))}
        self.client = FakeClient()
        self.fee_in_coin, self.stop_type, self.stop_price = fee_in_coin, stop_type, stop_price
        self.entry_status, self.stop_status, self.tp_status = entry_status, stop_status, tp_status
        self.prot_qty, self.opoco_error, self.sell_error = prot_qty, opoco_error, sell_error
        self.cancelled = False

    def install(self, plan):
        self.plan = plan
        fills = [{"price": "79990", "qty": str(plan.qty), "commission": self.fee_in_coin, "commissionAsset": "BTC"}] \
            if self.entry_status == "FILLED" else []
        e = report(self.ids[("entry",)], self.entry_status, qty=str(plan.qty), price=str(plan.entry_price),
                   fills=fills, quote=str(plan.qty * Dec("79990")) if self.entry_status == "FILLED" else "0")
        self.client.script(OPOCO, self.opoco_error or {"orderListId": 7, "orderReports": [e]})
        self.client.script(CANCEL_LIST, {"orderListId": 7})
        self.client.script(ORDER, self.sell_error or report(self.ids[("exit",)], "FILLED", side="SELL",
                                                            type_="MARKET", qty=str(plan.qty), quote="4900"))
        self.entry_report = e
        return self

    def send(self, method, endpoint, params):
        if (method, endpoint) == QUERY:
            cid = params["origClientOrderId"]
            self.client.calls.append((method, endpoint, dict(params)))
            received = self.plan.qty - Dec(self.fee_in_coin)
            q = str(self.prot_qty if self.prot_qty is not None else received)
            if cid == self.ids[("entry",)]:
                return self.entry_report
            if cid == self.ids[("protect", 0)]:
                status = "CANCELED" if self.cancelled and self.stop_status == "NEW" else self.stop_status
                if status == "MISSING":
                    raise sdk_error("BadRequestError", -2013, "Order does not exist.")
                return report(cid, status, side="SELL", type_=self.stop_type, qty=q, price="0",
                              stop=str(self.stop_price or self.plan.stop_price))
            if cid == self.ids[("protect", 1)]:
                status = "CANCELED" if self.cancelled and self.tp_status == "NEW" else self.tp_status
                return report(cid, status, side="SELL", type_="LIMIT_MAKER", qty=q, price=str(self.plan.take_profit_price))
        if (method, endpoint) == CANCEL_LIST:
            self.cancelled = True
        return self.client.send(method, endpoint, params)

    def count(self, key):
        return self.client.count(key)


def run(intent, state, market, cfg, rules=None, **ex):
    rules = rules or parse_symbol(exchange_info_symbol())
    iid = new_intent_id()
    ex_ = Exchange(iid, **ex)
    plan = entry.plan_entry(entry_mod.evaluate(intent, state, market, rules, cfg, "majors"), rules, cfg, iid)
    ex_.install(plan)
    trader = BinanceTrader(environment="testnet", client=ex_, _sleep=lambda s: None)
    out = entry.submit(intent, state, market, rules, cfg, trader, iid, "majors", sleep=lambda s: None)
    return out, ex_


# ---------------------------------------------------------------- the happy path
def test_risk_engine_then_one_protected_list(intent, state, market, cfg):
    out, ex = run(intent, state, market, cfg)
    assert out.status == entry.PROTECTED and not out.block_entries
    (opoco,) = [p for m, e, p in ex.client.calls if (m, e) == OPOCO]
    p = out.plan
    assert opoco["workingType"] == "LIMIT" and opoco["workingTimeInForce"] == "FOK" and opoco["workingSide"] == "BUY"
    assert opoco["pendingSide"] == "SELL" and opoco["pendingAboveType"] == "LIMIT_MAKER"
    assert opoco["pendingBelowType"] == "STOP_LOSS" and "pendingBelowPrice" not in opoco
    assert Dec(opoco["workingQuantity"]) == out.decision.qty                  # the risk engine's size
    assert (Dec(opoco["workingPrice"]), Dec(opoco["pendingBelowStopPrice"]), Dec(opoco["pendingAbovePrice"])) == \
        (Dec(out.decision.details["entry"]), Dec(out.decision.details["stop"]), Dec(out.decision.details["take_profit"]))
    assert {opoco["listClientOrderId"], opoco["workingClientOrderId"], opoco["pendingBelowClientOrderId"],
            opoco["pendingAboveClientOrderId"]} == {p.list_client_id, p.entry_client_id, p.stop_client_id,
                                                    p.take_profit_client_id}
    assert ex.count(ORDER) == 0 and ex.count(OPOCO) == 1                      # nothing else was sent


def test_a_rejected_idea_sends_nothing(intent, state, market, cfg):
    from dataclasses import replace
    blocked = replace(state, mode=Mode.ENTRIES_BLOCKED)
    rules = parse_symbol(exchange_info_symbol())
    client = FakeClient()
    out = entry.submit(intent, blocked, market, rules, cfg, BinanceTrader(environment="testnet", client=client),
                       new_intent_id(), "majors")
    assert out.status == entry.REJECTED and client.calls == []


def test_the_engine_runs_on_every_submit(intent, state, market, cfg, monkeypatch):
    calls = []
    real = entry_mod.evaluate
    monkeypatch.setattr(entry_mod, "evaluate", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    run(intent, state, market, cfg)
    assert len(calls) == 2                                                    # once in the test's plan, once in submit


def test_fee_in_the_coin_is_taken_off_what_must_be_protected(intent, state, market, cfg):
    out, _ = run(intent, state, market, cfg, fee_in_coin="0.00006")
    assert out.status == entry.PROTECTED and out.received_qty == out.decision.qty - Dec("0.00006")


def test_without_a_market_stop_the_limit_sits_at_the_stop_slippage_allowance(intent, state, market, cfg):
    info = exchange_info_symbol()
    info["orderTypes"] = [t for t in info["orderTypes"] if t != "STOP_LOSS"]
    rules = parse_symbol(info)
    out, ex = run(intent, state, market, cfg, rules=rules, stop_type="STOP_LOSS_LIMIT")
    (opoco,) = [p for m, e, p in ex.client.calls if (m, e) == OPOCO]
    stop = Dec(out.decision.details["stop"])
    assert opoco["pendingBelowType"] == "STOP_LOSS_LIMIT" and opoco["pendingBelowTimeInForce"] == "GTC"
    assert Dec(opoco["pendingBelowPrice"]) == (stop * (1 - Dec("0.005")) / Dec("0.01")).to_integral_value(
        rounding="ROUND_DOWN") * Dec("0.01")                                 # BTC allowance 0.5%, on the tick
    assert out.status == entry.PROTECTED


# ---------------------------------------------------------------- nothing bought, or refused
def test_fill_or_kill_that_expires_leaves_nothing(intent, state, market, cfg):
    out, ex = run(intent, state, market, cfg, entry_status="EXPIRED")
    assert out.status == entry.NOT_FILLED and not out.block_entries and ex.count(ORDER) == 0


def test_a_coin_without_opoco_or_any_stop_type_is_refused_before_sending(intent, state, market, cfg):
    info = exchange_info_symbol()
    info["opoAllowed"] = False
    out, ex = run(intent, state, market, cfg, rules=parse_symbol(info))
    assert out.status == entry.REFUSED and "OPOCO" in out.reason and ex.count(OPOCO) == 0
    info = exchange_info_symbol()
    info["orderTypes"] = ["LIMIT", "LIMIT_MAKER", "MARKET"]
    out, ex = run(intent, state, market, cfg, rules=parse_symbol(info))
    assert out.status == entry.REFUSED and "stop type" in out.reason and ex.count(OPOCO) == 0


def test_too_small_to_protect_is_refused(intent, state, market, cfg):
    rules = parse_symbol(exchange_info_symbol(min_notional="5"))
    iid = new_intent_id()
    d = entry_mod.evaluate(intent, state, market, rules, cfg, "majors")
    plan = entry.plan_entry(d, rules, cfg, iid)
    from dataclasses import replace
    tiny = replace(plan, qty=Dec("0.00007"), stop_price=Dec("70000"))        # 5.60 USDT bought: a valid buy
    assert tiny.problems(rules) == [p for p in tiny.problems(rules) if "too small to protect" in p] != []
    assert not any("too small" in p for p in replace(tiny, qty=Dec("0.0001")).problems(rules))   # stop leg 6.99


def test_binance_refusing_the_list_leaves_nothing(intent, state, market, cfg):
    out, ex = run(intent, state, market, cfg,
                  opoco_error=sdk_error("BadRequestError", -2010, "Account has insufficient balance."))
    assert out.status == entry.REFUSED and ex.count(ORDER) == 0 and not out.block_entries


# ---------------------------------------------------------------- protection not confirmed: sell, block
@pytest.mark.parametrize("ex, why", [
    (dict(stop_status="MISSING"), "does not exist"),
    (dict(stop_status="PENDING_NEW"), "not NEW"),
    (dict(stop_price=Dec("70000")), "protective prices"),
    (dict(prot_qty=Dec("0.005")), "cover"),                                  # the engine bought 0.00966
    (dict(stop_type="STOP_LOSS_LIMIT"), "types"),
])
def test_unconfirmed_protection_means_sell_now_and_block(intent, state, market, cfg, ex, why):
    out, x = run(intent, state, market, cfg, **ex)
    assert out.status == entry.EMERGENCY_EXIT and out.block_entries and why in out.notes[0]
    assert x.cancelled and x.count(ORDER) == 1
    (sell,) = [p for m, e, p in x.client.calls if (m, e) == ORDER]
    assert sell["side"] == "SELL" and sell["type"] == "MARKET" and Dec(sell["quantity"]) == out.decision.qty


def test_a_failed_emergency_sale_says_act_now(intent, state, market, cfg):
    out, _ = run(intent, state, market, cfg, stop_status="MISSING",
                 sell_error=sdk_error("BadRequestError", -2010, "Account has insufficient balance."))
    assert out.status == entry.UNPROTECTED and out.block_entries and "by hand" in out.reason


def test_a_protective_order_already_filled_counts_as_closed(intent, state, market, cfg):
    out, x = run(intent, state, market, cfg, tp_status="FILLED")
    assert out.status == entry.PROTECTED and "closed" in out.reason and x.count(ORDER) == 0


def test_an_unknown_entry_outcome_is_passed_on_never_resent(intent, state, market, cfg):
    rules = parse_symbol(exchange_info_symbol())
    client = FakeClient().script(OPOCO, sdk_error("NetworkError")).script(
        QUERY, sdk_error("BadRequestError", -2013, "Order does not exist."))
    with pytest.raises(OrderStatusUnknown):
        entry.submit(intent, state, market, rules, cfg,
                     BinanceTrader(environment="testnet", client=client, _sleep=lambda s: None),
                     new_intent_id(), "majors")
    assert client.count(OPOCO) == 1


# ---------------------------------------------------------------- only entry.submit places entries
def test_only_submit_places_a_protected_entry_and_it_calls_evaluate_first():
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).as_posix()
        if ".place_protected_entry(" in f.read_text():
            assert rel == "execution/entry.py", rel
    tree = ast.parse((SRC / "execution" / "entry.py").read_text())
    submit = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "submit")
    names = [getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(submit) if isinstance(n, ast.Call)]
    assert names.index("evaluate") < names.index("place_protected_entry")
    assert not any(a.arg == "decision" for a in submit.args.args)             # no ready-made decision accepted


# ---------------------------------------------------------------- round trips
def test_a_clean_round_trip_and_the_journal(intent, state, market, cfg, tmp_path):
    out, x = run(intent, state, market, cfg)
    rules = parse_symbol(exchange_info_symbol())
    rt = roundtrip.close(out, BinanceTrader(environment="testnet", client=x, _sleep=lambda s: None), rules)
    assert rt.clean and rt.close.status == "FILLED" and {rt.stop_after.status, rt.take_profit_after.status} == {"CANCELED"}
    assert roundtrip.record(tmp_path, rt, 1.0) == 1
    row = json.loads((tmp_path / roundtrip.JOURNAL).read_text())
    assert row["clean"] and row["stop_type"] == "STOP_LOSS" and row["symbol"] == "BTCUSDT"


def test_a_round_trip_closed_by_its_take_profit_is_clean_without_a_sale(intent, state, market, cfg, tmp_path):
    out, x = run(intent, state, market, cfg)
    x.tp_status = "FILLED"
    rt = roundtrip.close(out, BinanceTrader(environment="testnet", client=x, _sleep=lambda s: None),
                         parse_symbol(exchange_info_symbol()))
    assert rt.clean and rt.close is None


def test_unclean_round_trips_do_not_count(intent, state, market, cfg, tmp_path):
    out, x = run(intent, state, market, cfg, entry_status="EXPIRED")
    rt = roundtrip.close(out, BinanceTrader(environment="testnet", client=x), parse_symbol(exchange_info_symbol()))
    assert not rt.clean and roundtrip.record(tmp_path, rt, 1.0) == 0
    out, x = run(intent, state, market, cfg)
    x.send_orig = x.send

    def never_cancels(method, endpoint, params):
        if (method, endpoint) == CANCEL_LIST:
            return {"orderListId": 7}                                       # Binance says OK but nothing changes
        return x.send_orig(method, endpoint, params)
    x.send = never_cancels
    rt = roundtrip.close(out, BinanceTrader(environment="testnet", client=x), parse_symbol(exchange_info_symbol()))
    assert not rt.clean and "still open" in rt.problems[0]
    assert roundtrip.record(tmp_path, rt, 2.0) == 0


# ---------------------------------------------------------------- bot testnet-roundtrip
def test_the_command_runs_one_round_trip_and_counts_it(tmp_path, monkeypatch):
    iid = new_intent_id()
    import bot.core.ids as ids
    monkeypatch.setattr(ids, "new_intent_id", lambda: iid)
    ex = Exchange(iid)
    ex.client.script(("GET", "/api/v3/account"), {"canTrade": True, "canWithdraw": False, "accountType": "SPOT",
                                                  "permissions": ["SPOT"], "balances": [
                                                      {"asset": "USDT", "free": "10000", "locked": "0"}]})
    ex.client.script(("GET", "/api/v3/openOrders"), [])

    class Trader(BinanceTrader):
        def place_protected_entry(self, req, rules, fee_rate=Dec("0.001")):
            ex.install(req)
            return super().place_protected_entry(req, rules, fee_rate)

    from bot.execution.ledger import Ledger
    book = Ledger.load(tmp_path, "testnet")
    book.baseline = {"BTC": Dec(0)}                                         # 5c: reconciled before and after
    book.save()
    args = argparse.Namespace(config="config/default.yaml", symbol="BTCUSDT", stop_pct="1.5", equity="1000",
                              hold=0, journal_dir=str(tmp_path))
    out = io.StringIO()
    code = cli._testnet_roundtrip(args, make_trader=lambda **k: Trader(environment="testnet", client=ex),
                                  make_reader=lambda **k: FakeMarket(), load_creds=lambda: object(),
                                  sleep=lambda s: None, out=out)
    text = out.getvalue()
    assert code == 0, text
    assert "Risk engine .... accepted" in text and "fill-or-kill" in text and "CLEAN round trip" in text
    assert "Clean round trips so far: 1 of 5" in text
    (opoco,) = [p for m, e, p in ex.client.calls if (m, e) == OPOCO]
    assert Dec(opoco["workingQuantity"]) * Dec(opoco["workingPrice"]) < 1000 * Dec("0.25")   # sized on 1,000 USDT
