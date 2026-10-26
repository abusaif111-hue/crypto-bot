"""Module 5c: the ledger (written before an order is sent) and reconciliation against Binance
(read-only; anything that differs blocks new entries)."""
import argparse
import ast
import io
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot.core.ids import new_intent_id
from bot.core.money import plain
from bot.exchange.binance import ExchangeError
from bot.exchange.trading import (TRADES_PAGE, AccountInfo, BinanceTrader, OrderState, OrderStatusUnknown,
                                  TradeFill, TradeRecord)
from bot.execution import reconcile as rc
from bot.execution.ledger import CLOSED, LEDGER, NOT_OPENED, OPEN, PENDING, Ledger, LedgerError
from bot.portfolio.accounting import Balance
from tests.fakes import FakeMarket
from tests.unit.test_protected_entry import OPOCO, Exchange

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
NOW = 1_792_800_000.0
TOL = {"BTC": Dec("0.00001")}


def order(cid, status, side="SELL", type_="STOP_LOSS", qty="0.01", executed=None, quote="0", order_id=1):
    ex = Dec(executed) if executed is not None else (Dec(qty) if status == "FILLED" else Dec(0))
    return OrderState("BTCUSDT", order_id, cid, status, side, type_, Dec(qty), ex, Dec(quote), Dec("0"))


class Venue:
    """Binance as reconciliation sees it: orders by client ID, open orders, balances, fills. Anything else
    (placing, cancelling) fails the test."""

    def __init__(self, btc="1"):
        self.orders, self.open, self.fills = {}, [], {}
        self.balances = {"BTC": Dec(btc), "USDT": Dec("10000")}
        self.fail = False
        self.history = []                          # TradeRecord fills (myTrades by time)

    def query(self, symbol, cid):
        if self.fail:
            raise ExchangeError("testnet down")
        return self.orders.get(cid)

    def open_orders(self, symbol=None):
        return list(self.open)

    def account(self):
        return AccountInfo(True, False, "SPOT", ("SPOT",), tuple(Balance(a, q, Dec(0)) for a, q in self.balances.items()))

    def my_trades(self, symbol, order_id):
        return self.fills.get(order_id, [])

    def trades_between(self, symbol, start_ms, end_ms):
        self.history_asked = (symbol, start_ms, end_ms)
        return [r for r in self.history if start_ms <= r.time_ms <= end_ms]

    def __getattr__(self, name):
        raise AssertionError(f"reconciliation called {name}")


def book(tmp_path, btc="1"):
    b = Ledger.load(tmp_path, "testnet")
    b.baseline = {"BTC": Dec(btc)}
    return b


def open_trade(b, qty="0.01", created=NOW - 1000):
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", created)
    b.mark(t.intent_id, OPEN, created, received_qty=Dec(qty))
    return t


def protected(v, t, qty="0.01"):
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "NEW", qty=qty)
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "NEW", type_="LIMIT_MAKER", qty=qty)
    v.open += [v.orders[t.ids["stop"]], v.orders[t.ids["take-profit"]]]


# ---------------------------------------------------------------- the ledger
def test_a_missing_ledger_starts_empty_and_saves_and_loads(tmp_path):
    b = Ledger.load(tmp_path, "testnet")
    assert not b.trades and not b.baseline
    b.set_baseline({"BTC": Dec("1.5")}, "Sultan", "testnet account as Binance gave it", NOW)
    t = open_trade(b)
    b.save()
    again = Ledger.load(tmp_path, "testnet")
    assert again.baseline == {"BTC": Dec("1.5")} and again.trades[t.intent_id].status == OPEN
    assert again.trades[t.intent_id].received_qty == Dec("0.01") and "Sultan" in again.baseline_note
    assert again.expected_holdings() == {"BTC": Dec("1.51")}


@pytest.mark.parametrize("text", ["{not json", json.dumps({"version": 9}), json.dumps(
    {"version": 1, "environment": "testnet", "trades": [{"intent_id": "x", "status": "MAYBE"}]})])
def test_an_unreadable_ledger_is_an_error_and_never_reset(tmp_path, text):
    (tmp_path / LEDGER).write_text(text)
    with pytest.raises(LedgerError, match="never reset"):
        Ledger.load(tmp_path, "testnet")
    assert (tmp_path / LEDGER).read_text() == text


def test_a_ledger_is_for_one_environment(tmp_path):
    book(tmp_path).save()
    with pytest.raises(LedgerError, match="testnet"):
        Ledger.load(tmp_path, "live")


def test_trades_move_only_forward_and_an_intent_is_added_once(tmp_path):
    b = book(tmp_path)
    iid = new_intent_id()
    t = b.add_pending(iid, "BTCUSDT", "BTC", NOW)
    with pytest.raises(LedgerError, match="never sent twice"):
        b.add_pending(iid, "BTCUSDT", "BTC", NOW)
    b.mark(t.intent_id, NOT_OPENED, NOW)
    for status in (OPEN, PENDING, CLOSED):
        with pytest.raises(LedgerError, match="cannot go"):
            b.mark(t.intent_id, status, NOW)


def test_no_trade_without_a_baseline_for_its_coin(tmp_path):
    with pytest.raises(LedgerError, match="no baseline for ETH"):
        book(tmp_path).add_pending(new_intent_id(), "ETHUSDT", "ETH", NOW)


def test_the_baseline_needs_an_operator_a_reason_and_nothing_open(tmp_path):
    b = book(tmp_path)
    with pytest.raises(LedgerError, match="written reason"):
        b.set_baseline({"BTC": Dec(1)}, "Sultan", "short", NOW)
    t = open_trade(b)
    with pytest.raises(LedgerError, match="PENDING or OPEN"):
        b.set_baseline({"BTC": Dec(1)}, "Sultan", "testnet account was reset", NOW)
    b.mark(t.intent_id, CLOSED, NOW, left_qty=Dec("0.000004"))
    assert b.expected_holdings() == {"BTC": Dec("1.000004")}          # dust a close left behind
    b.set_baseline({"BTC": Dec("1.000004")}, "Sultan", "testnet account was reset", NOW)
    assert b.expected_holdings() == {"BTC": Dec("1.000004")}          # dust is now in the baseline, not twice


# ---------------------------------------------------------------- reconciliation: coins and open orders
def test_nothing_open_and_the_coins_as_expected_is_clean(tmp_path):
    rec = rc.reconcile(book(tmp_path), Venue(), TOL, NOW)
    assert rec.clean and rec.faults() == [] and any("BTC: 1 as expected" in c for c in rec.checked)


@pytest.mark.parametrize("btc,clean", [("1.00001", True), ("0.99999", True), ("1.00002", False), ("0.9", False)])
def test_coins_must_match_within_one_step(tmp_path, btc, clean):
    rec = rc.reconcile(book(tmp_path), Venue(btc=btc), TOL, NOW)
    assert rec.clean is clean
    if not clean:
        assert "BTC: Binance holds" in rec.mismatches[0] and rec.faults()[0][0] == "RECONCILIATION_MISMATCH"


def test_an_open_order_that_is_not_the_bots_is_a_mismatch(tmp_path):
    v = Venue()
    v.open.append(order("web_123", "NEW", side="BUY", type_="LIMIT"))
    rec = rc.reconcile(book(tmp_path), v, TOL, NOW)
    assert not rec.clean and "not one of the bot's" in rec.mismatches[0]


def test_an_order_left_open_by_a_closed_trade_is_a_mismatch(tmp_path):
    b = book(tmp_path)
    t = open_trade(b)
    b.mark(t.intent_id, CLOSED, NOW)
    v = Venue()
    v.open.append(order(t.ids["stop"], "NEW"))
    rec = rc.reconcile(b, v, TOL, NOW)
    assert "which is CLOSED" in rec.mismatches[0]


# ---------------------------------------------------------------- open trades
def test_an_open_trade_with_both_protective_orders_is_clean_and_its_coins_count(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = open_trade(b)
    protected(v, t)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and not rec.updates, rec
    assert rc.reconcile(b, Venue(btc="1"), TOL, NOW).clean is False        # its coins are missing


def test_protection_smaller_than_the_coins_held_is_unprotected(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = open_trade(b)
    protected(v, t, qty="0.005")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert "covers 0.005" in rec.unprotected[0] and rec.faults()[0][0] == "UNPROTECTED_POSITION"


def test_a_stop_that_filled_closes_the_trade_and_records_the_dust(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.000004")
    t = open_trade(b, qty="0.010004")
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "FILLED", qty="0.01")
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "EXPIRED", type_="LIMIT_MAKER")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean, rec
    (u,) = rec.updates
    assert u.status == CLOSED and u.left_qty == Dec("0.000004") and "stop-loss" in u.note
    rc.apply(b, rec, NOW)
    assert Ledger.load(tmp_path, "testnet").trades[t.intent_id].status == CLOSED


def test_one_protective_order_filled_while_the_other_is_still_open_is_a_mismatch(tmp_path):
    b, v = book(tmp_path), Venue()
    t = open_trade(b)
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "FILLED", type_="LIMIT_MAKER")
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "NEW")
    v.open.append(v.orders[t.ids["stop"]])
    rec = rc.reconcile(b, v, TOL, NOW)
    assert any(f"{t.ids['stop']} is still NEW" in m for m in rec.mismatches)


def test_both_protective_orders_filled_is_a_mismatch(tmp_path):
    b, v = book(tmp_path), Venue()
    t = open_trade(b)
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "FILLED")
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "FILLED", type_="LIMIT_MAKER")
    assert "both protective orders filled" in rc.reconcile(b, v, TOL, NOW).mismatches[0]


def test_protection_gone_after_the_bots_own_sale_closes_the_trade(tmp_path):
    b, v = book(tmp_path), Venue()
    t = open_trade(b)
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "CANCELED")
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "CANCELED", type_="LIMIT_MAKER")
    v.orders[t.ids["exit"]] = order(t.ids["exit"], "FILLED", type_="MARKET")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and rec.updates[0].status == CLOSED and "market sale" in rec.updates[0].note


@pytest.mark.parametrize("stop,exit_", [(None, None), ("CANCELED", None), ("CANCELED", "EXPIRED")])
def test_coins_without_protection_or_a_filled_sale_are_unprotected(tmp_path, stop, exit_):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = open_trade(b)
    if stop:
        v.orders[t.ids["stop"]] = order(t.ids["stop"], stop)
    if exit_:
        v.orders[t.ids["exit"]] = order(t.ids["exit"], exit_, type_="MARKET")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert "no confirmed protection" in rec.unprotected[0] and not rec.updates
    assert rec.faults()[0] == ("UNPROTECTED_POSITION", rec.unprotected[0])


# ---------------------------------------------------------------- pending trades (outcome not recorded)
def test_a_pending_entry_not_found_yet_waits_and_blocks(tmp_path):
    b, v = book(tmp_path), Venue(btc="5")                 # the coins are not compared while unsettled
    b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW - 10)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.clean and rec.unsettled and not rec.mismatches and not rec.updates
    assert rec.faults()[0][0] == "RECONCILIATION_MISMATCH"


def test_a_pending_entry_never_found_after_the_wait_was_never_placed(tmp_path):
    b = book(tmp_path)
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW - rc.SETTLE_S)
    rec = rc.reconcile(b, Venue(), TOL, NOW)
    assert rec.clean and rec.updates[0].status == NOT_OPENED
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == NOT_OPENED


# ---------------------------------------------------------------- PR-1: not found after the wait is not proof
def old_pending(b, age=rc.SETTLE_S + 1):
    return b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW - age)


def fill(t, buyer=True, qty="0.01", after_s=2):
    return TradeRecord(order_id=99, is_buyer=buyer, qty=Dec(qty), time_ms=int((t.created_at + after_s) * 1000))


def test_never_placed_only_when_every_record_agrees(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and [u.status for u in rec.updates] == [NOT_OPENED] and "never placed" in rec.updates[0].note
    assert v.history_asked == ("BTCUSDT", int(t.created_at * 1000) - rc.FILL_MARGIN_MS, int(NOW * 1000))


@pytest.mark.parametrize("kind", ["stop", "take-profit", "exit"])
def test_a_protective_or_closing_order_of_the_trade_means_uncertain(tmp_path, kind):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.orders[t.ids[kind]] = order(t.ids[kind], "CANCELED")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.clean and not rec.updates and "uncertain" in rec.mismatches[0] and kind in rec.mismatches[0]
    assert rec.faults()[0][0] == "RECONCILIATION_MISMATCH"
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == PENDING                  # never assumed placed or not placed


def test_a_buy_fill_since_the_trade_was_written_means_uncertain(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill(t)]
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates and "1 buy fill(s) on BTCUSDT" in rec.mismatches[0] and "0.01 BTC" in rec.mismatches[0]


def test_sell_fills_and_fills_before_the_trade_are_not_evidence(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill(t, buyer=False), fill(t, after_s=-(rc.FILL_MARGIN_MS / 1000) - 5)]
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and rec.updates[0].status == NOT_OPENED


def test_more_coins_than_the_ledger_expects_means_uncertain(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.0012")                       # baseline 1 BTC; 0.0012 more is there
    old_pending(b)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates and "holds 0.0012 more BTC than the ledger expects" in rec.mismatches[0]


def test_coins_within_one_step_are_not_evidence(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.00001")
    old_pending(b)
    assert rc.reconcile(b, v, TOL, NOW).updates[0].status == NOT_OPENED


def test_too_many_fills_to_check_means_uncertain(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)
    v.history = [fill(t, buyer=False)] * TRADES_PAGE
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates and "too many to check" in rec.mismatches[0]


def test_the_fill_search_never_asks_for_more_than_24_hours(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b, age=3 * 86_400)
    rc.reconcile(b, v, TOL, NOW)
    _, start, end = v.history_asked
    assert start == int(t.created_at * 1000) - rc.FILL_MARGIN_MS and end - start == rc.FILL_WINDOW_MS < 86_400_000


def test_an_exchange_error_while_gathering_evidence_settles_nothing(tmp_path):
    b, v = book(tmp_path), Venue()
    t = old_pending(b)

    def down(symbol, start_ms, end_ms):
        raise ExchangeError("myTrades: no answer")
    v.trades_between = down
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.errors and not rec.updates
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == PENDING


def test_an_uncertain_trade_is_settled_later_by_a_definitive_record(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = old_pending(b)
    assert "uncertain" in rc.reconcile(b, v, TOL, NOW).mismatches[0]
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", quote="800")
    protected(v, t)
    rec = rc.reconcile(b, v, TOL, NOW + 60)
    assert rec.clean and [u.status for u in rec.updates] == [OPEN]
    rc.apply(b, rec, NOW + 60)
    assert b.trades[t.intent_id].status == OPEN and b.trades[t.intent_id].received_qty == Dec("0.01")


def test_a_pending_entry_that_expired_did_not_open(tmp_path):
    b, v = book(tmp_path), Venue()
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "EXPIRED", side="BUY", type_="LIMIT")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and rec.updates[0].status == NOT_OPENED


def test_a_pending_entry_still_open_is_a_mismatch(tmp_path):
    b, v = book(tmp_path), Venue()
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "NEW", side="BUY", type_="LIMIT")
    v.open.append(v.orders[t.ids["entry"]])
    assert "should never stay open" in rc.reconcile(b, v, TOL, NOW).mismatches[0]


def test_a_pending_entry_that_filled_is_found_and_its_coins_worked_out_from_the_fills(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.00999")
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", qty="0.01", order_id=42)
    v.fills[42] = [TradeFill(Dec("80000"), Dec("0.01"), Dec("0.00001"), "BTC")]
    protected(v, t, qty="0.00999")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean, rec
    (u,) = rec.updates
    assert u.status == OPEN and u.received_qty == Dec("0.00999")
    rc.apply(b, rec, NOW)
    assert b.trades[t.intent_id].status == OPEN and b.expected_holdings()["BTC"] == Dec("1.00999")


def test_a_pending_entry_that_filled_and_has_no_protection_is_unprotected_and_tracked(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", qty="0.01")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.unprotected and rec.updates[0].status == OPEN and not rec.mismatches   # coins accounted for


def test_a_pending_entry_that_filled_and_closed_already_goes_straight_to_closed(tmp_path):
    b, v = book(tmp_path), Venue()
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    v.orders[t.ids["entry"]] = order(t.ids["entry"], "FILLED", side="BUY", type_="LIMIT", qty="0.01")
    v.orders[t.ids["take-profit"]] = order(t.ids["take-profit"], "FILLED", type_="LIMIT_MAKER", qty="0.01")
    v.orders[t.ids["stop"]] = order(t.ids["stop"], "EXPIRED")
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.clean and [u.status for u in rec.updates] == [CLOSED]
    rc.apply(b, rec, NOW)                                  # PENDING -> CLOSED is allowed
    assert b.trades[t.intent_id].status == CLOSED


# ---------------------------------------------------------------- failing closed, read-only
def test_an_exchange_error_discards_every_update_and_blocks(tmp_path):
    b, v = book(tmp_path), Venue()
    b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW - rc.SETTLE_S)
    v.fail = True
    rec = rc.reconcile(b, v, TOL, NOW)
    assert rec.errors and not rec.updates and rec.faults() == [("EXCHANGE_UNAVAILABLE", rec.errors[0])]
    rc.apply(b, rec, NOW)
    assert not (tmp_path / LEDGER).exists()


def test_reconciliation_only_reads():
    """It looks up and reads; it never places, cancels or tests an order."""
    tree = ast.parse((SRC / "execution" / "reconcile.py").read_text())
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "trader"}
    # trades_between (PR-1) reads the same myTrades endpoint by time; test_trading_adapter proves it only sends GETs
    assert called == {"query", "open_orders", "account", "my_trades", "trades_between"}, called
    assert set(rc.ReadOnlyTrader.__dict__) >= called
    for name in ("place", "cancel", "cancel_list", "place_protected_entry", "test_order"):
        assert f".{name}(" not in (SRC / "execution" / "reconcile.py").read_text()


def test_plain_numbers_never_use_an_exponent():
    assert plain(Dec("81450.00")) == "81450" and plain(Dec("10")) == "10" and plain(Dec("0.00010")) == "0.0001"
    for f in ("cli.py",):
        assert ".normalize()}" not in (SRC / f).read_text()        # the display bug: bid 8.145E+4


# ---------------------------------------------------------------- bot reconcile
def rc_args(tmp_path, **kw):
    base = dict(config="config/default.yaml", journal_dir=str(tmp_path), set_baseline=False,
                symbols=["BTCUSDT"], operator=None, reason=None)
    return argparse.Namespace(**{**base, **kw})


def run_rc(tmp_path, venue, **kw):
    out = io.StringIO()
    code = cli._reconcile_cmd(rc_args(tmp_path, **kw), make_trader=lambda **k: venue,
                              make_reader=lambda **k: FakeMarket(), load_creds=lambda: object(), out=out)
    return code, out.getvalue()


def test_the_command_records_a_baseline_then_finds_the_account_clean(tmp_path, capsys):
    v = Venue(btc="1.5")
    code, _ = run_rc(tmp_path, v)
    assert code == 2 and "no baseline yet" in capsys.readouterr().err
    code, text = run_rc(tmp_path, v, set_baseline=True, operator="Sultan", reason="testnet account as given")
    assert code == 0 and "BTC 1.5 (not the bot's)" in text and "ETH 0" in text
    code, text = run_rc(tmp_path, v)
    assert code == 0 and "CLEAN. New entries may go ahead." in text and "OK: BTC: 1.5 as expected" in text
    v.balances["BTC"] = Dec("1.4")
    code, text = run_rc(tmp_path, v)
    assert code == 1 and "NOT CLEAN" in text and "Binance holds 1.4" in text


def test_no_baseline_while_orders_are_open(tmp_path, capsys):
    v = Venue()
    v.open.append(order("web_1", "NEW"))
    code, _ = run_rc(tmp_path, v, set_baseline=True, operator="Sultan", reason="testnet account as given")
    assert code == 2 and "open orders" in capsys.readouterr().err and not (tmp_path / LEDGER).exists()


def test_the_command_says_act_now_for_unprotected_coins(tmp_path):
    b = book(tmp_path, btc="1")
    open_trade(b)
    b.save()
    code, text = run_rc(tmp_path, Venue(btc="1.01"))
    assert code == 3 and "UNPROTECTED:" in text


# ---------------------------------------------------------------- bot testnet-roundtrip with the ledger
def rt_args(tmp_path):
    return argparse.Namespace(config="config/default.yaml", symbol="BTCUSDT", stop_pct="1.5", equity="1000",
                              hold=0, journal_dir=str(tmp_path))


def roundtrip_exchange(iid, open_orders=()):
    ex = Exchange(iid)
    ex.client.script(("GET", "/api/v3/account"), {"canTrade": True, "canWithdraw": False, "accountType": "SPOT",
                                                  "permissions": ["SPOT"], "balances": [
                                                      {"asset": "USDT", "free": "10000", "locked": "0"}]})
    ex.client.script(("GET", "/api/v3/openOrders"), list(open_orders))

    class Trader(BinanceTrader):
        def place_protected_entry(self, req, rules, fee_rate=Dec("0.001")):
            ex.install(req)
            return super().place_protected_entry(req, rules, fee_rate)
    return ex, Trader


def run_rt(tmp_path, monkeypatch, open_orders=(), trader_cls=None):
    iid = new_intent_id()
    import bot.core.ids as ids
    monkeypatch.setattr(ids, "new_intent_id", lambda: iid)
    ex, Trader = roundtrip_exchange(iid, open_orders)
    Trader = trader_cls(Trader) if trader_cls else Trader
    out = io.StringIO()
    code = cli._testnet_roundtrip(rt_args(tmp_path), make_trader=lambda **k: Trader(environment="testnet", client=ex),
                                  make_reader=lambda **k: FakeMarket(), load_creds=lambda: object(),
                                  sleep=lambda s: None, out=out)
    return code, out.getvalue(), ex, iid


def test_a_round_trip_needs_a_baseline_first(tmp_path, monkeypatch, capsys):
    code, _, ex, _ = run_rt(tmp_path, monkeypatch)
    assert code == 2 and "no baseline for BTC" in capsys.readouterr().err and ex.count(OPOCO) == 0


def test_a_round_trip_is_not_started_when_reconciliation_is_not_clean(tmp_path, monkeypatch):
    book(tmp_path, btc="0").save()
    stray = {"symbol": "BTCUSDT", "orderId": 5, "clientOrderId": "web_9", "status": "NEW", "side": "BUY",
             "type": "LIMIT", "origQty": "0.1", "executedQty": "0", "price": "70000"}
    code, text, ex, _ = run_rt(tmp_path, monkeypatch, open_orders=[stray])
    assert code == 2 and "NOT started" in text and "not one of the bot's" in text and ex.count(OPOCO) == 0


def test_a_clean_round_trip_is_in_the_ledger_and_reconciled_after(tmp_path, monkeypatch):
    book(tmp_path, btc="0").save()
    code, text, ex, iid = run_rt(tmp_path, monkeypatch)
    assert code == 0, text
    assert "Before check.... reconciliation CLEAN" in text and "After check..... reconciliation CLEAN" in text
    assert "Filled ......... bought" in text and " at 79990.00 " in text and "P&L ............" in text
    assert "E+" not in text and "CLEAN round trip" in text
    t = Ledger.load(tmp_path, "testnet").trades[iid.hex]
    assert t.status == CLOSED and t.left_qty == 0


def test_the_trade_is_in_the_ledger_before_its_order_is_sent(tmp_path, monkeypatch):
    book(tmp_path, btc="0").save()
    seen = {}

    def unknown(Base):
        class T(Base):
            def place_protected_entry(self, req, rules, fee_rate=Dec("0.001")):
                seen["ledger"] = json.loads((tmp_path / LEDGER).read_text())
                raise OrderStatusUnknown(req.symbol, req.entry_client_id, "timeout")
        return T
    code, text, ex, iid = run_rt(tmp_path, monkeypatch, trader_cls=unknown)
    assert code == 3 and "stays PENDING" in text
    assert [t["status"] for t in seen["ledger"]["trades"]] == [PENDING]
    assert Ledger.load(tmp_path, "testnet").trades[iid.hex].status == PENDING


def test_a_round_trip_the_risk_engine_rejects_is_recorded_as_not_opened(tmp_path, monkeypatch):
    book(tmp_path, btc="0").save()
    import bot.execution.entry as entry_mod
    from bot.risk.models import Decision, RejectCode
    monkeypatch.setattr(entry_mod, "evaluate", lambda *a, **k: Decision.reject(RejectCode.DAILY_LOSS, "no"))
    code, text, ex, iid = run_rt(tmp_path, monkeypatch)
    assert code == 1 and ex.count(OPOCO) == 0
    assert Ledger.load(tmp_path, "testnet").trades[iid.hex].status == NOT_OPENED


def test_updates_are_never_applied_from_a_run_with_errors(tmp_path):
    b = book(tmp_path)
    t = b.add_pending(new_intent_id(), "BTCUSDT", "BTC", NOW)
    rc.apply(b, rc.Reconciliation(errors=["down"], updates=[rc.Update(t.intent_id, NOT_OPENED)]), NOW)
    assert b.trades[t.intent_id].status == PENDING and not (tmp_path / LEDGER).exists()


def test_a_round_trip_is_not_clean_when_the_account_differs_afterwards(tmp_path, monkeypatch):
    book(tmp_path, btc="0").save()

    def coins_appear(Base):
        class T(Base):
            def account(self):
                info = super().account()
                if getattr(self, "_seen", 0) >= 2:                  # after the trade: 0.5 BTC nobody expects
                    info = AccountInfo(info.can_trade, info.can_withdraw, info.account_type, info.permissions,
                                       info.balances + (Balance("BTC", Dec("0.5"), Dec(0)),))
                self._seen = getattr(self, "_seen", 0) + 1
                return info
        return T
    code, text, ex, iid = run_rt(tmp_path, monkeypatch, trader_cls=coins_appear)
    assert code == 1 and "After check..... reconciliation NOT CLEAN" in text and "Binance holds 0.5" in text
    assert "NOT clean. Clean round trips so far: 0 of 5" in text
