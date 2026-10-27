"""PR-2 (audit H2 and M1): a sale is recorded before it is sent, each attempt has its own client ID, and a new
attempt is sent only when every earlier one is known. Fakes only: nothing here reaches Binance."""
import inspect
import json
from decimal import Decimal as Dec

import pytest

from bot.exchange.trading import AccountInfo, OrderRejected, OrderState, OrderStatusUnknown
from bot.execution import entry, exit as exits, reconcile as rc, roundtrip
from bot.execution.ledger import (EXIT_SETTLE_S, LEDGER, MAX_EXIT_ATTEMPTS, OPEN, PENDING, VERSION, Ledger,
                                  LedgerError)
from bot.portfolio.accounting import Balance
from tests.unit.test_reconcile import NOW, TOL, Venue, book, order
from tests.unit.test_trading_adapter import RULES

QTY = Dec("0.0012")


def sale(cid, status, executed, n=1):
    return OrderState("BTCUSDT", n, cid, status, "SELL", "MARKET", QTY, Dec(executed), Dec(executed) * 80000, Dec(0))


class Exchange:
    """Orders by client ID and a BTC balance. A sale can fill, fill but stay hidden, expire, or be refused."""

    def __init__(self, btc="1.0012"):
        self.orders, self.hidden, self.sent = {}, set(), []
        self.btc = Dec(btc)
        self.on_sale, self.partial = "fill", Dec(0)

    def cancel_list(self, symbol, list_cid):
        return True

    def query(self, symbol, cid):
        return None if cid in self.hidden else self.orders.get(cid)

    def account(self):
        return AccountInfo(True, False, "SPOT", ("SPOT",), (Balance("BTC", self.btc, Dec(0)),))

    def place(self, req, rules):
        self.sent.append(req.client_order_id)
        how = self.on_sale
        if how == "reject":
            raise OrderRejected("Binance refused the order: MIN_NOTIONAL")
        if how == "unknown_nothing":
            raise OrderStatusUnknown(req.symbol, req.client_order_id, "read timed out")
        done = {"fill": req.qty, "unknown_filled": req.qty, "expire_partial": self.partial, "expire_none": Dec(0)}[how]
        self.btc -= done
        self.orders[req.client_order_id] = sale(req.client_order_id, "FILLED" if done == req.qty else "EXPIRED", done,
                                                n=len(self.orders) + 1)
        if how == "unknown_filled":
            self.hidden.add(req.client_order_id)
            raise OrderStatusUnknown(req.symbol, req.client_order_id, "read timed out")
        return self.orders[req.client_order_id]


@pytest.fixture
def setup(tmp_path):
    b = book(tmp_path)                                               # baseline: 1 BTC that is not the bot's
    t = b.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", NOW - 3600)
    b.mark(t.intent_id, OPEN, NOW - 3600, received_qty=QTY)
    b.save()
    return b, t, Exchange()


def close(b, t, ex, now, record=None):
    held = b.expected_holdings()["BTC"] - t.received_qty

    def note(k):
        b.note_exit_attempt(t.intent_id, k, now)
        b.save()
    return exits.close_trade(t, ex, RULES, now, record or note, held)


# ---------------------------------------------------------------- never the same coins twice
def test_a_sale_that_filled_but_stays_hidden_is_never_sent_again(setup):
    b, t, ex = setup
    ex.on_sale = "unknown_filled"
    r = close(b, t, ex, NOW)
    assert not r.closed and r.attempt == 0 and t.exit_attempts == 1
    r = close(b, t, ex, NOW + 60)                                    # young: wait, send nothing
    assert r.waiting and "cannot be found yet" in r.problem
    r = close(b, t, ex, NOW + EXIT_SETTLE_S + 100)                   # old, and the coins are gone: send nothing
    assert not r.closed and not r.waiting and "it may have been executed" in r.problem
    ex.hidden.clear()                                                # it shows up at last
    r = close(b, t, ex, NOW + EXIT_SETTLE_S + 160)
    assert r.closed and r.left_qty == 0
    assert ex.sent == [t.exit_ids()[0]]                              # exactly one sale


def test_a_sale_never_placed_is_tried_again_under_a_new_id(setup):
    b, t, ex = setup
    ex.on_sale = "unknown_nothing"
    assert close(b, t, ex, NOW).attempt == 0
    assert close(b, t, ex, NOW + 60).waiting                         # not before EXIT_SETTLE_S
    ex.on_sale = "fill"
    r = close(b, t, ex, NOW + EXIT_SETTLE_S + 1)                     # still not found, the coins are still there
    assert r.closed and r.attempt == 1
    assert ex.sent == [t.exit_ids()[0], t.exit_ids()[1]]
    assert len(set(ex.sent)) == len(ex.sent)                         # no ID sent twice


def test_a_partly_filled_sale_is_followed_by_one_for_the_rest(setup):
    b, t, ex = setup
    ex.on_sale, ex.partial = "expire_partial", Dec("0.0005")
    r = close(b, t, ex, NOW)
    assert not r.closed and "sold by the next attempt" in r.problem
    ex.on_sale = "fill"
    r = close(b, t, ex, NOW + 60)
    assert r.closed and r.left_qty == 0 and r.sale.executed_qty == Dec("0.0007")


def test_at_most_three_attempts_then_sell_by_hand(setup):
    b, t, ex = setup
    ex.on_sale = "expire_none"
    for k in range(MAX_EXIT_ATTEMPTS):
        assert close(b, t, ex, NOW + 60 * k).attempt == k
    r = close(b, t, ex, NOW + 600)
    assert not r.closed and "sell them by hand" in r.problem
    assert ex.sent == t.exit_ids()                                   # three IDs, each once, then nothing


def test_an_open_sale_is_waited_for(setup):
    b, t, ex = setup
    ex.orders[t.exit_ids()[0]] = sale(t.exit_ids()[0], "NEW", "0")
    r = close(b, t, ex, NOW)
    assert r.waiting and "still NEW" in r.problem and not ex.sent


def test_a_sale_is_recorded_before_it_is_sent(setup):
    b, t, ex = setup
    seen = []

    def record(k):
        seen.append((k, list(ex.sent)))                              # nothing sent yet when it is recorded
        b.note_exit_attempt(t.intent_id, k, NOW)
        b.save()
    close(b, t, ex, NOW, record)
    assert seen == [(0, [])] and Ledger.load(b.path.parent, "testnet").trades[t.intent_id].exit_attempts == 1


def test_a_sale_that_cannot_be_recorded_is_not_sent(setup):
    b, t, ex = setup

    def broken(k):
        raise OSError("disk full")
    r = close(b, t, ex, NOW, broken)
    assert not r.closed and "nothing was sent" in r.problem and not ex.sent


def test_a_refused_sale_counts_as_an_attempt(setup):
    b, t, ex = setup
    ex.on_sale = "reject"
    r = close(b, t, ex, NOW)
    assert not r.closed and "failed" in r.problem and t.exit_attempts == 1


def test_a_sale_sent_before_attempts_were_kept_is_found(setup):
    b, t, ex = setup                                                  # a version-1 trade: no attempt recorded
    ex.orders[t.exit_ids()[0]] = sale(t.exit_ids()[0], "EXPIRED", "0")
    r = close(b, t, ex, NOW)
    assert r.closed and r.attempt == 1 and ex.sent == [t.exit_ids()[1]]


# ---------------------------------------------------------------- the ledger
def test_attempts_only_go_forward_and_stop_at_the_cap(setup):
    b, t, _ = setup
    b.note_exit_attempt(t.intent_id, 0, NOW)
    with pytest.raises(LedgerError):
        b.note_exit_attempt(t.intent_id, 0, NOW)                     # an ID is never reused
    b.note_exit_attempt(t.intent_id, 2, NOW)
    with pytest.raises(LedgerError):
        b.note_exit_attempt(t.intent_id, MAX_EXIT_ATTEMPTS, NOW)
    b.mark(t.intent_id, "CLOSED", NOW)
    with pytest.raises(LedgerError, match="nothing of it is sold"):
        b.note_exit_attempt(t.intent_id, 2, NOW)


def test_a_version_1_ledger_loads_and_is_saved_as_version_2(tmp_path):
    body = {"version": 1, "environment": "testnet", "baseline": {"BTC": "1"}, "baseline_note": "",
            "trades": [{"intent_id": "a" * 32, "symbol": "BTCUSDT", "base_asset": "BTC", "status": "OPEN",
                        "created_at": 1, "updated_at": 1, "received_qty": "0.0012", "left_qty": "0"}]}
    (tmp_path / LEDGER).write_text(json.dumps(body))
    b = Ledger.load(tmp_path, "testnet")
    t = b.trades["a" * 32]
    assert (t.exit_attempts, t.exit_sent_at) == (0, 0.0)
    b.save()
    saved = json.loads((tmp_path / LEDGER).read_text())
    assert saved["version"] == VERSION == 2 and saved["trades"][0]["exit_attempts"] == 0
    (tmp_path / LEDGER).write_text(json.dumps({**body, "version": 3}))
    with pytest.raises(LedgerError, match="version 3"):
        Ledger.load(tmp_path, "testnet")


def test_every_attempt_id_is_the_trades_own():
    b = Ledger(__import__("pathlib").Path("/nonexistent"), "testnet")
    b.baseline = {"BTC": Dec(1)}
    t = b.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", NOW)
    assert t.exit_ids()[0] == t.ids["exit"] and set(t.exit_ids()) <= set(t.ids.values())
    assert len(set(t.exit_ids())) == MAX_EXIT_ATTEMPTS


# ---------------------------------------------------------------- reconciliation
def open_trade(b, attempts=0, sent_at=0.0):
    t = b.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", NOW - 3600)
    b.mark(t.intent_id, OPEN, NOW - 3600, received_qty=Dec("0.01"))
    t.exit_attempts, t.exit_sent_at = attempts, sent_at
    return t


def test_a_sale_in_flight_is_waited_for_not_sold_again(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = open_trade(b, attempts=1, sent_at=NOW - 60)                   # protection cancelled, sale recorded, not found
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.unprotected and "cannot be found yet" in rec.unsettled[0]
    rec = rc.reconcile(b, v, TOL, NOW - 60 + EXIT_SETTLE_S + 1)       # after the wait: unprotected, the runner decides
    assert rec.unprotected_ids == [t.intent_id]


def test_what_all_attempts_sold_counts(tmp_path):
    b, v = book(tmp_path), Venue(btc="1")
    t = open_trade(b, attempts=2, sent_at=NOW - 60)
    v.orders[t.exit_ids()[0]] = order(t.exit_ids()[0], "EXPIRED", type_="MARKET", qty="0.01", executed="0.004")
    v.orders[t.exit_ids()[1]] = order(t.exit_ids()[1], "FILLED", type_="MARKET", qty="0.006", order_id=2)
    rec = rc.reconcile(b, v, TOL, NOW)
    [u] = rec.updates
    assert u.status == "CLOSED" and u.left_qty == 0 and rec.clean


def test_an_earlier_filled_sale_does_not_close_a_trade_with_a_later_attempt_in_flight(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.006")
    t = open_trade(b, attempts=2, sent_at=NOW - 60)
    v.orders[t.exit_ids()[0]] = order(t.exit_ids()[0], "FILLED", type_="MARKET", qty="0.004")   # sold only part
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not rec.updates and t.exit_ids()[1] in rec.unsettled[0] and "cannot be found yet" in rec.unsettled[0]


def test_selling_more_than_the_trade_held_is_a_mismatch(tmp_path):
    b, v = book(tmp_path), Venue(btc="0.99")
    t = open_trade(b, attempts=2, sent_at=NOW - 600)
    v.orders[t.exit_ids()[0]] = order(t.exit_ids()[0], "FILLED", type_="MARKET", qty="0.01")
    v.orders[t.exit_ids()[1]] = order(t.exit_ids()[1], "FILLED", type_="MARKET", qty="0.01", order_id=2)
    rec = rc.reconcile(b, v, TOL, NOW)
    assert any("more than the" in m for m in rec.mismatches)


def test_an_open_later_attempt_is_the_bots_own_order(tmp_path):
    b, v = book(tmp_path), Venue(btc="1.01")
    t = open_trade(b, attempts=2, sent_at=NOW - 30)
    o = order(t.exit_ids()[1], "NEW", type_="MARKET", qty="0.01")
    v.orders[t.exit_ids()[1]] = o
    v.open = [o]
    rec = rc.reconcile(b, v, TOL, NOW)
    assert not any("not one of the bot's" in m for m in rec.mismatches)
    assert "still NEW" in rec.unsettled[0]


# ---------------------------------------------------------------- every sale is recorded first
@pytest.mark.parametrize("fn,record", [(exits.close_trade, "record_attempt("),
                                       (entry._emergency_exit, "record_exit(0)"),
                                       (roundtrip.close, "record_exit(0)")])
def test_every_sale_is_recorded_before_it_is_sent(fn, record):
    src = inspect.getsource(fn)
    assert src.index(record) < src.index(".place(")


def test_the_runner_and_the_round_trip_pass_the_recorder():
    from bot import cli
    from bot.execution.runner import TestnetRunner
    assert "record_exit=" in inspect.getsource(TestnetRunner._enter)
    assert "_record_exit_attempt" in inspect.getsource(TestnetRunner._close)
    src = inspect.getsource(cli._testnet_roundtrip)
    assert "record_exit=record_exit" in src and "roundtrip.close(outcome, trader, rules, record_exit)" in src


def test_pending_status_is_kept_for_an_emergency_sale_record(setup):
    b, _, _ = setup
    t = b.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", NOW)
    b.note_exit_attempt(t.intent_id, 0, NOW)                          # an emergency sale while PENDING
    assert t.status == PENDING and t.exit_attempts == 1


# ---------------------------------------------------------------- the emergency sale inside an entry
def scripted(intent, state, market, cfg, **kw):
    from bot.core.ids import new_intent_id
    from bot.exchange.filters import parse_symbol
    from bot.exchange.trading import BinanceTrader
    from tests.conftest import exchange_info_symbol
    from tests.unit.test_protected_entry import Exchange as Scripted
    rules, iid = parse_symbol(exchange_info_symbol()), new_intent_id()
    x = Scripted(iid, **kw)
    x.install(entry.plan_entry(entry.evaluate(intent, state, market, rules, cfg, "majors"), rules, cfg, iid))
    return x, rules, iid, BinanceTrader(environment="testnet", client=x, _sleep=lambda s: None)


def test_an_emergency_sale_is_recorded_before_it_is_sent(intent, state, market, cfg):
    from tests.unit.test_protected_entry import ORDER
    x, rules, iid, trader = scripted(intent, state, market, cfg, stop_status="MISSING")
    seen = []
    out = entry.submit(intent, state, market, rules, cfg, trader, iid, "majors", sleep=lambda s: None,
                       record_exit=lambda k: seen.append((k, x.count(ORDER))))
    assert out.status == entry.EMERGENCY_EXIT and seen == [(0, 0)] and x.count(ORDER) == 1


def test_an_emergency_sale_that_cannot_be_recorded_is_not_sent(intent, state, market, cfg):
    from tests.unit.test_protected_entry import ORDER
    x, rules, iid, trader = scripted(intent, state, market, cfg, stop_status="MISSING")

    def broken(k):
        raise OSError("disk full")
    out = entry.submit(intent, state, market, rules, cfg, trader, iid, "majors", sleep=lambda s: None,
                       record_exit=broken)
    assert out.status == entry.UNPROTECTED and "NOT sent" in out.reason and x.count(ORDER) == 0


def test_a_sale_waiting_for_its_outcome_is_a_yellow_alert():
    from bot import alerts
    a = alerts.describe("live", {"event": "exit_waiting", "symbol": "BTCUSDT", "trade": "abc", "attempt": 0,
                                 "reason": "cannot be found yet"})
    assert a.level == alerts.YELLOW and "nothing more is sent" in a.text and a.key == "exit_waiting:abc:0"
