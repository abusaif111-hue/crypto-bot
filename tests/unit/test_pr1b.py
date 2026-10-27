"""PR-1b: whose order a fill is (by Binance's order ID), unreadable Binance replies, and the read-only
`pending` command. Fakes only: nothing here reaches Binance or places an order."""
import argparse
import ast
import inspect
import io
from dataclasses import replace
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import alerts, cli
from bot.core.ids import client_order_id, is_bot_client_id, new_intent_id
from bot.exchange.binance import ExchangeError
from bot.exchange.trading import MalformedResponse, OrderStatusUnknown, TradeRecord
from bot.execution import reconcile as rc
from bot.execution.ledger import LEDGER, NOT_OPENED, OPEN, PENDING, Ledger
from tests.fakes import FakeMarket
from tests.unit.test_reconcile import NOW, TOL, Venue, book, old_pending, order, protected
from tests.unit.test_trading_adapter import RULES, FakeClient, buy, filled, trader

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"


def fill_of(t, order_id, buyer=True, qty="0.01", after_s=2, fee="0", fee_asset="USDT"):
    return TradeRecord(order_id, buyer, Dec(qty), int((t.created_at + after_s) * 1000), Dec(fee), fee_asset)


def settled(rec, t):
    return [u.status for u in rec.updates if u.intent_id == t.intent_id]


def uncertain(rec):
    return [m for m in rec.mismatches if "uncertain" in m]


# ---------------------------------------------------------------- a fill that is this trade's entry
def test_a_fill_of_this_trades_entry_settles_it_from_the_actual_order(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = old_pending(b)
    v.history = [fill_of(t, 77)]
    v.by_id[77] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", quote="800", order_id=77)
    protected(v, t)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean, rec.mismatches
    [u] = rec.updates
    assert u.status == OPEN and u.received_qty == Dec("0.01") and "found from its fill, order 77" in u.note
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == OPEN and b.trades[t.intent_id].entry_price == Dec(80000)
    assert not rc.reconcile(b, v, TOL, NOW + 60).updates                # repeated: nothing settled twice


@pytest.mark.parametrize("why,kw", [
    ("a SELL", dict(side="SELL")),
    ("still open", dict(status="NEW")),
    ("nothing bought", dict(status="EXPIRED", executed="0")),
])
def test_the_entry_id_alone_never_settles_a_trade(tmp_path, why, kw):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = old_pending(b)
    v.history = [fill_of(t, 77, buyer=kw.get("side", "BUY") == "BUY")]
    args = dict(status="FILLED", side="BUY", type_="LIMIT", quote="800", order_id=77)
    args.update(kw)
    v.by_id[77] = order(t.ids["entry"], args.pop("status"), **args)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates, why
    assert uncertain(rec) and "carries this trade's entry ID" in uncertain(rec)[0]


def test_two_orders_with_the_entry_id_are_uncertain(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.02")
    t = old_pending(b)
    v.history = [fill_of(t, 77), fill_of(t, 78)]
    for oid in (77, 78):
        v.by_id[oid] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", quote="800", order_id=oid)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates and "2 orders carry this trade's entry ID" in uncertain(rec)[0]


# ---------------------------------------------------------------- a fill that is not the bot's
def test_a_manual_buy_is_not_evidence_and_its_coins_are_allowed_for(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")                         # baseline 1 + the manual 0.01
    t = old_pending(b)
    v.history = [fill_of(t, 55)]
    v.by_id[55] = order("web_8f3a1c", "FILLED", side="BUY", type_="MARKET", order_id=55)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert settled(rec, t) == [NOT_OPENED] and not uncertain(rec)
    assert any(m.startswith("BTC: Binance holds 1.01") for m in rec.mismatches)   # the manual buy is still flagged


def test_a_fee_taken_in_the_coin_is_allowed_for_exactly(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.00999")                      # bought 0.01, paid 0.00001 BTC fee
    t = old_pending(b)
    v.history = [fill_of(t, 55, fee="0.00001", fee_asset="BTC")]
    v.by_id[55] = order("web_8f3a1c", "FILLED", side="BUY", type_="MARKET", order_id=55)
    rec = rc.reconcile(b, v, {"BTC": Dec(0)}, NOW)                     # no tolerance at all: exact
    assert settled(rec, t) == [NOT_OPENED] and not uncertain(rec)


def test_coins_beyond_what_a_manual_buy_explains_are_still_uncertain(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.02")                         # the manual buy explains only 0.01
    t = old_pending(b)
    v.history = [fill_of(t, 55)]
    v.by_id[55] = order("web_8f3a1c", "FILLED", side="BUY", type_="MARKET", order_id=55)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not settled(rec, t) and "holds 0.01 more BTC than the ledger expects after allowing" in uncertain(rec)[0]


def test_a_manual_sale_cannot_hide_coins_an_entry_bought(tmp_path):
    # Entry executed unseen (+0.01) and someone sold 0.01 of the baseline: the balance looks normal, but the sale
    # is counted, so the coins the entry bought still show.
    b, v = book(tmp_path), Venue(btc="1")
    t = old_pending(b)
    v.history = [fill_of(t, 55, buyer=False)]
    v.by_id[55] = order("web_sell", "FILLED", side="SELL", type_="MARKET", order_id=55)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not settled(rec, t) and "holds 0.01 more BTC" in uncertain(rec)[0]


def test_another_bot_trades_fills_are_not_evidence(tmp_path):
    b, v = book(tmp_path), Venue()
    done = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW - 5000)
    b.mark(done.intent_id, OPEN, NOW - 5000, received_qty=Dec("0.01"))
    b.mark(done.intent_id, "CLOSED", NOW - 4000, left_qty=Dec(0))
    t = old_pending(b)
    v.history = [fill_of(t, 66, buyer=False)]
    v.by_id[66] = order(done.ids["exit"], "FILLED", side="SELL", type_="MARKET", order_id=66)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert settled(rec, t) == [NOT_OPENED] and rec.clean


# ---------------------------------------------------------------- anything else is uncertain, never "not the bot's"
@pytest.mark.parametrize("case,setup,words", [
    ("order not found", lambda t, v: None, "Binance cannot find that order"),
    ("reply for another order", lambda t, v: v.by_id.__setitem__(
        55, order("web_x", "FILLED", side="BUY", order_id=56)), "the reply is for BTCUSDT order 56"),
    ("reply for another coin", lambda t, v: v.by_id.__setitem__(
        55, replace(order("web_x", "FILLED", side="BUY", order_id=55), symbol="ETHUSDT")),
     "the reply is for ETHUSDT order 55"),
    ("side does not match the fill", lambda t, v: v.by_id.__setitem__(
        55, order("web_x", "FILLED", side="SELL", order_id=55)), "do not match its side"),
    ("a bot-made ID this ledger does not know", lambda t, v: v.by_id.__setitem__(
        55, order(client_order_id(new_intent_id(), "entry"), "FILLED", side="BUY", order_id=55)),
     "bot-made ID"),
    ("this trade's own stop", lambda t, v: v.by_id.__setitem__(
        55, order(t.ids["stop"], "FILLED", side="BUY", order_id=55)), "is this trade's stop order"),
])
def test_an_unidentified_fill_keeps_the_trade_pending(tmp_path, case, setup, words):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill_of(t, 55)]
    setup(t, v)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not settled(rec, t), case
    assert uncertain(rec) and words in uncertain(rec)[0], uncertain(rec)
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == PENDING


@pytest.mark.parametrize("error", [ExchangeError("GET /api/v3/order: timeout"),
                                   MalformedResponse("order: Binance's reply could not be read")])
def test_a_failed_lookup_is_uncertain_not_proof(tmp_path, error):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill_of(t, 55)]

    def broken(symbol, order_id):
        raise error
    v.query_by_id = broken
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not settled(rec, t)
    assert f"could not be looked up ({type(error).__name__})" in uncertain(rec)[0]
    assert rec.faults()[0][0] == "RECONCILIATION_MISMATCH"              # red alert and a fault, entries blocked


def test_too_many_orders_to_identify_is_uncertain(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill_of(t, 1000 + i) for i in range(rc.ATTRIBUTE_MAX + 1)]
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not settled(rec, t) and "too many to identify" in uncertain(rec)[0]


def test_the_uncertain_text_is_the_same_every_round(tmp_path):
    b, v = book(tmp_path), Venue()
    old_pending(b)
    v.history = [fill_of(b.active()[0], 55)]
    texts = [rc.reconcile(b, v, TOL, NOW + dt).mismatches for dt in (0, 60, 3600)]
    assert texts[0] and all(x == texts[0] for x in texts)


def test_bot_ids_are_recognised():
    iid = new_intent_id()
    assert all(is_bot_client_id(client_order_id(iid, k)) for k in ("entry", "exit", "list"))
    assert is_bot_client_id(client_order_id(iid, "protect", 1))
    assert not any(is_bot_client_id(x) for x in ("web_8f3a1c", "and_123", "x-ABC", "sbe0" + "g" * 32, ""))


# ---------------------------------------------------------------- unreadable replies
def test_an_unreadable_reply_settles_nothing_and_is_reported(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)

    def garbled(symbol, start_ms, end_ms):
        raise MalformedResponse("myTrades: Binance's reply could not be read (TypeError: 'false' is not true or false)")
    v.trades_between = garbled
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.errors and rec.unreadable and not rec.updates and not rec.clean
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == PENDING


@pytest.mark.parametrize("path,reply,call", [
    (("GET", "/api/v3/order"), {"symbol": "BTCUSDT", "orderId": 1}, lambda tr: tr.query("BTCUSDT", "c")),
    (("GET", "/api/v3/order"), {**filled("c"), "executedQty": "NaN"}, lambda tr: tr.query("BTCUSDT", "c")),
    (("GET", "/api/v3/order"), {**filled("c"), "orderId": True}, lambda tr: tr.query_by_id("BTCUSDT", 1)),
    (("GET", "/api/v3/openOrders"), {"not": "a list"}, lambda tr: tr.open_orders(None)),
    (("GET", "/api/v3/openOrders"), [{"symbol": "BTCUSDT"}], lambda tr: tr.open_orders(None)),
    (("GET", "/api/v3/myTrades"), [{"orderId": 5, "isBuyer": "false", "qty": "1", "time": 1, "commission": "0",
                                    "commissionAsset": "USDT"}], lambda tr: tr.trades_between("BTCUSDT", 0, 9)),
    (("GET", "/api/v3/myTrades"), [{"orderId": 5, "isBuyer": True, "qty": "-1", "time": 1, "commission": "0",
                                    "commissionAsset": "USDT"}], lambda tr: tr.trades_between("BTCUSDT", 0, 9)),
    (("GET", "/api/v3/myTrades"), [{"orderId": 5, "isBuyer": True, "qty": "1", "time": 1}],
     lambda tr: tr.trades_between("BTCUSDT", 0, 9)),
    (("GET", "/api/v3/myTrades"), "garbage", lambda tr: tr.my_trades("BTCUSDT", 5)),
    (("GET", "/api/v3/account"), {"balances": [{"asset": "BTC", "free": "x", "locked": "0"}]},
     lambda tr: tr.account()),
    (("GET", "/api/v3/account"), ["not", "a", "dict"], lambda tr: tr.account()),
])
def test_every_unreadable_reply_is_a_malformed_response_never_a_crash(path, reply, call):
    with pytest.raises(MalformedResponse):
        call(trader(FakeClient().script(path, reply)))
    assert issubclass(MalformedResponse, ExchangeError)                  # every caller already fails closed on it


def test_a_strict_boolean_is_read_as_it_is():
    rows = [{"orderId": 5, "isBuyer": False, "qty": "0.5", "time": 7, "commission": "0.001",
             "commissionAsset": "BNB"}]
    [r] = trader(FakeClient().script(("GET", "/api/v3/myTrades"), rows)).trades_between("BTCUSDT", 0, 9)
    assert (r.order_id, r.is_buyer, r.qty, r.time_ms, r.commission, r.commission_asset) == \
        (5, False, Dec("0.5"), 7, Dec("0.001"), "BNB")


def test_query_by_id_is_one_get_of_the_allowed_order_endpoint():
    c = FakeClient().script(("GET", "/api/v3/order"), filled("c"))
    o = trader(c).query_by_id("BTCUSDT", 28)
    assert o.order_id == 28 and c.calls == [("GET", "/api/v3/order", {"symbol": "BTCUSDT", "orderId": 28})]


def test_query_by_id_not_found_is_none():
    from bot.exchange.trading import _Refused
    c = FakeClient().script(("GET", "/api/v3/order"), _Refused(-2013, "Order does not exist."))
    assert trader(c).query_by_id("BTCUSDT", 28) is None


def test_an_unreadable_order_reply_is_looked_up_never_sent_again():
    cid = client_order_id(new_intent_id(), "exit")
    c = FakeClient().script(("POST", "/api/v3/order"), {"garbled": True})
    c.script(("GET", "/api/v3/order"), filled(cid))
    got = trader(c).place(buy(cid=cid), RULES)
    assert got.client_order_id == cid and c.count(("POST", "/api/v3/order")) == 1


def test_an_unreadable_order_reply_that_cannot_be_found_is_unknown():
    cid = client_order_id(new_intent_id(), "exit")
    c = FakeClient().script(("POST", "/api/v3/order"), {"garbled": True})
    c.script(("GET", "/api/v3/order"), {"also": "garbled"})
    with pytest.raises(OrderStatusUnknown):
        trader(c).place(buy(cid=cid), RULES)
    assert c.count(("POST", "/api/v3/order")) == 1


# ---------------------------------------------------------------- the runner keeps going and says so
def test_an_unreadable_reply_alert_is_red():
    a = alerts.describe("live", {"event": "exchange_data_error", "reason": "openOrders: not a list"})
    assert a.level == alerts.RED and "could not read" in a.text and "openOrders" in a.text


# ---------------------------------------------------------------- the read-only pending command
class OnlyReads:
    """A Venue that fails the test on anything but reading."""

    def __init__(self, v):
        self._v = v

    def __getattr__(self, name):
        if name not in ("query", "query_by_id", "open_orders", "account", "my_trades", "trades_between"):
            raise AssertionError(f"the pending command called {name}")
        return getattr(self._v, name)


def run_pending(state_dir, venue, now=NOW):
    out = io.StringIO()
    args = argparse.Namespace(config="config/default.yaml", state_dir=str(state_dir), env="testnet")
    code = cli._pending_cmd(args, make_trader=lambda **k: OnlyReads(venue), make_reader=lambda **k: FakeMarket(),
                            load_creds=lambda: object(), out=out, now=now)
    return code, out.getvalue()


def test_pending_shows_every_record_and_changes_nothing(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    b.save()
    v.history = [fill_of(t, 55), fill_of(t, 56, buyer=False)]
    v.by_id[55] = order("web_8f3a1c", "FILLED", side="BUY", type_="MARKET", order_id=55)
    before = (tmp_path / LEDGER).read_bytes()
    code, text = run_pending(tmp_path, v)
    assert code == 1
    assert t.ids["entry"] in text and "not found by its client ID" in text
    assert "order 55: 1 fill(s), bought 0.01" in text and "not the bot's" in text
    assert "order 56" in text and "EVIDENCE" in text and "Binance cannot find that order" in text
    assert "Reconciliation would record: keep it PENDING" in text and "Nothing was changed" in text
    assert (tmp_path / LEDGER).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [LEDGER]


def test_pending_says_what_reconciliation_would_record(tmp_path):
    b, v = book(tmp_path), Venue()
    old_pending(b)
    b.save()
    code, text = run_pending(tmp_path, v)
    assert code == 1 and "Reconciliation would record: NOT_OPENED" in text
    assert Ledger.load(tmp_path, "testnet").active()[0].status == PENDING    # ... but did not record it


def test_pending_with_nothing_pending(tmp_path):
    book(tmp_path).save()
    assert run_pending(tmp_path, Venue())[0] == 0


def test_the_pending_command_cannot_write_or_resume():
    src = inspect.getsource(cli._pending_cmd)
    tree = ast.parse(src.lstrip())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for bad in ("save", "apply", "mark", "place", "cancel", "cancel_list", "place_protected_entry", "resume",
                "set_baseline", "write_text", "unlink"):
        assert bad not in calls, bad
    assert "need_switch" not in src                                      # live: never with the live switch


def test_pending_is_a_subcommand_for_live_and_testnet():
    for env in ("live", "testnet"):
        with pytest.raises(SystemExit):
            cli.main([env, "pending", "--help"])
