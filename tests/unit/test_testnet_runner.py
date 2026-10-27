"""Module 5d: the testnet runner. A capped account inside the testnet account, exits that never sell
twice, and the loop: reconcile, mark, requests, time stops, then the strategy once per candle, with
every entry written to the ledger first and sent through `entry.submit()` (the risk engine)."""
import argparse
import io
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot.core.config import AppConfig
from bot.exchange.binance import ExchangeError
from bot.exchange.filters import parse_symbol
from bot.exchange.trading import AccountInfo, OrderRejected, OrderState, TradeFill
from bot.execution import exit as exits
from bot.execution.account import ACCOUNT_FILE, AccountError, TradingAccount
from bot.execution.ledger import CLOSED, NOT_OPENED, OPEN, PENDING, Ledger
from bot.execution.runner import CLOCK_FILE, JOURNAL_FILE, REQUESTS_DIR, RunnerError, TestnetRunner
from bot.market.data import MarketDataService
from bot.portfolio.accounting import Balance
from bot.risk.models import Mode, TradeIntent
from bot.strategies.base import Exit
from tests.conftest import exchange_info_symbol
from tests.fakes import Clock, FakeMarket

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
RULES = parse_symbol(exchange_info_symbol("BTCUSDT"))
T0 = 1_792_627_200.0 + 4 * 3600 + 30            # 2026-10-22 04:00:30 UTC, just after a 4h candle closed


class FakeBinance:
    """A testnet account that keeps balances and orders: OPOCO entries fill at the ask, protective orders
    wait, cancels and market sales work. Every order it was sent is in `sent`."""
    environment = "testnet"

    def __init__(self, usdt="10000", btc="1", ask="80000", bid="79990"):
        self.bal = {"USDT": Dec(usdt), "BTC": Dec(btc), "ETH": Dec("1")}
        self.orders: dict[str, OrderState] = {}
        self.ask, self.bid = Dec(ask), Dec(bid)
        self.sent: list[tuple[str, str]] = []
        self.fail_reads = False
        self.sale_error: Exception | None = None

    def _put(self, cid, **kw):
        base = dict(symbol="BTCUSDT", order_id=len(self.orders) + 1, client_order_id=cid, status="NEW",
                    side="SELL", type="LIMIT", orig_qty=Dec(0), executed_qty=Dec(0), quote_qty=Dec(0),
                    price=Dec(0), fills=(), stop_price=None, order_list_id=7)
        base.update(kw)
        self.orders[cid] = OrderState(**base)
        return self.orders[cid]

    def place_protected_entry(self, req, rules, fee_rate=Dec("0.001")):
        self.sent.append(("opoco", req.symbol))
        if req.entry_price < self.ask:
            return self._put(req.entry_client_id, side="BUY", status="EXPIRED", orig_qty=req.qty)
        cost = req.qty * self.ask
        self.bal["USDT"] -= cost
        self.bal["BTC"] += req.qty
        self._put(req.stop_client_id, type=req.stop_type, orig_qty=req.qty, stop_price=req.stop_price)
        self._put(req.take_profit_client_id, type="LIMIT_MAKER", orig_qty=req.qty, price=req.take_profit_price)
        return self._put(req.entry_client_id, side="BUY", status="FILLED", orig_qty=req.qty, executed_qty=req.qty,
                         quote_qty=cost, price=req.entry_price,
                         fills=(TradeFill(self.ask, req.qty, Dec(0), "BTC"),))

    def query(self, symbol, cid):
        if self.fail_reads:
            raise ExchangeError("testnet down")
        return self.orders.get(cid)

    def query_by_id(self, symbol, order_id):
        if self.fail_reads:
            raise ExchangeError("testnet down")
        return next((o for o in self.orders.values() if o.order_id == order_id), None)

    def cancel_list(self, symbol, list_cid):
        self.sent.append(("cancel", list_cid))
        hit = False
        for cid, o in list(self.orders.items()):
            if o.status == "NEW" and o.type in ("STOP_LOSS", "STOP_LOSS_LIMIT", "LIMIT_MAKER") and \
                    cid[4:] == list_cid[4:]:
                self.orders[cid] = OrderState(**{**o.__dict__, "status": "CANCELED"})
                hit = True
        return hit

    def place(self, req, rules):
        self.sent.append(("sell", req.client_order_id))
        if self.sale_error:
            raise self.sale_error
        self.bal["BTC"] -= req.qty
        self.bal["USDT"] += req.qty * self.bid
        return self._put(req.client_order_id, type="MARKET", status="FILLED", orig_qty=req.qty,
                         executed_qty=req.qty, quote_qty=req.qty * self.bid)

    def fill_stop(self, cid, price):
        o = self.orders[cid]
        self.bal["BTC"] -= o.orig_qty
        self.bal["USDT"] += o.orig_qty * Dec(price)
        self.orders[cid] = OrderState(**{**o.__dict__, "status": "FILLED", "executed_qty": o.orig_qty,
                                         "quote_qty": o.orig_qty * Dec(price)})
        for other, p in list(self.orders.items()):
            if other != cid and other[4:] == cid[4:] and p.status == "NEW":
                self.orders[other] = OrderState(**{**p.__dict__, "status": "EXPIRED"})

    def open_orders(self, symbol=None):
        if self.fail_reads:
            raise ExchangeError("testnet down")
        return [o for o in self.orders.values() if o.status == "NEW"]

    def account(self):
        if self.fail_reads:
            raise ExchangeError("testnet down")
        return AccountInfo(True, False, "SPOT", ("SPOT",), tuple(Balance(a, q, Dec(0)) for a, q in self.bal.items()))

    def my_trades(self, symbol, order_id):
        return []

    def trades_between(self, symbol, start_ms, end_ms):
        if self.fail_reads:
            raise ExchangeError("testnet down")
        return [r for r in getattr(self, "history", []) if start_ms <= r.time_ms <= end_ms]


class Idea:
    """A strategy that proposes what it is told, once per 4h candle."""
    name, interval = "H2b", "4h"

    def __init__(self):
        self.ideas, self.exits, self.calls = [], [], 0

    def propose(self, data, now):
        self.calls += 1
        out, self.ideas = self.ideas, []
        return out

    def manage(self, data, now, open_trades):
        out, self.exits = [a for a in self.exits if a.symbol in {t.symbol for t in open_trades}], []
        return out


def intent(entry="80080", stop="78000", hours=720):
    e, s = Dec(entry), Dec(stop)
    return TradeIntent("BTCUSDT", "BUY", e, s, e + 3 * (e - s), "H2b", hours, "2 of 3 returns negative")


@pytest.fixture
def world(tmp_path):
    cfg = AppConfig()
    clock = Clock(T0)
    ex = FakeBinance()
    market = FakeMarket()
    book = Ledger.load(tmp_path, "testnet")
    book.set_baseline({"BTC": Dec("1"), "ETH": Dec("1")}, "Sultan", "testnet account as given", T0)
    book.save()
    acct = TradingAccount.begin(tmp_path, cfg, book, {a: q for a, q in ex.bal.items()}, ex.bal["USDT"],
                                Dec("1000"), "Sultan", T0)
    s = Idea()
    data = MarketDataService(market, cfg, clock)
    r = TestnetRunner(tmp_path, cfg, ex, data, data, [s], {"BTCUSDT": "majors", "ETHUSDT": "majors"}, book, acct,
                      clock=clock)
    return argparse.Namespace(runner=r, ex=ex, s=s, clock=clock, dir=tmp_path, cfg=cfg, market=market)


def journal(d):
    return [json.loads(x) for x in (d / JOURNAL_FILE).read_text().splitlines()] if (d / JOURNAL_FILE).exists() else []


def events(d, name):
    return [e for e in journal(d) if e["event"] == name]


def request(d, kind, reason="start the testnet runner"):
    (d / REQUESTS_DIR).mkdir(exist_ok=True)
    (d / REQUESTS_DIR / f"{len(list((d / REQUESTS_DIR).iterdir()))}.json").write_text(
        json.dumps({"type": kind, "operator": "Sultan", "reason": reason}))


def resume(w):
    request(w.dir, "resume")
    w.runner.tick()
    assert w.runner.account.modes.mode is Mode.RUNNING, journal(w.dir)


def next_candle(w):
    w.clock.advance(4 * 3600)


# ---------------------------------------------------------------- the account
def test_equity_is_the_capital_plus_what_changed_since_the_start(world):
    a = world.runner.account
    start = dict(world.ex.bal)
    assert a.equity(start, {"BTCUSDT": Dec("80000")}) == Dec("1000")
    later = {**start, "USDT": start["USDT"] - Dec("800"), "BTC": start["BTC"] + Dec("0.01")}
    assert a.equity(later, {"BTCUSDT": Dec("79000")}) == Dec("990")           # 1000 - 800 + 0.01 x 79000
    assert a.cash(later) == Dec("200")


def test_the_risk_engine_sees_the_bots_cash_not_the_whole_account(world):
    st = world.runner._state(T0, True)
    assert st.equity == 1000 and st.free_quote == 1000 and st.positions == ()


def test_the_account_starts_once_with_a_baseline_nothing_open_and_enough_usdt(world, tmp_path_factory):
    with pytest.raises(AccountError, match="started once"):
        TradingAccount.begin(world.dir, world.cfg, world.runner.ledger, {}, Dec("10000"), Dec("1"), "Sultan", T0)
    d = tmp_path_factory.mktemp("other")
    book = Ledger.load(d, "testnet")
    with pytest.raises(AccountError, match="no baseline"):
        TradingAccount.begin(d, world.cfg, book, {}, Dec("10000"), Dec("1000"), "Sultan", T0)
    book.baseline = {"BTC": Dec(1)}
    with pytest.raises(AccountError, match="at most the free USDT"):
        TradingAccount.begin(d, world.cfg, book, {}, Dec("500"), Dec("1000"), "Sultan", T0)
    book.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", T0)
    with pytest.raises(AccountError, match="PENDING or OPEN"):
        TradingAccount.begin(d, world.cfg, book, {}, Dec("10000"), Dec("1000"), "Sultan", T0)


def test_a_restart_keeps_breakers_and_blocks_entries(world):
    a = world.runner.account
    a.modes.mode = Mode.RUNNING
    a.modes.active_breaches.add("REJ_DRAWDOWN_BREAKER")
    a.save()
    again = TradingAccount.load(world.dir, world.cfg, T0 + 60)
    assert again.modes.mode is Mode.ENTRIES_BLOCKED and "REJ_DRAWDOWN_BREAKER" in again.modes.active_breaches
    assert again.capital == 1000 and again.start == a.start


def test_an_unreadable_account_is_an_error_and_never_reset(world):
    (world.dir / ACCOUNT_FILE).write_text("{oops")
    with pytest.raises(AccountError, match="never reset"):
        TradingAccount.load(world.dir, world.cfg, T0)
    assert (world.dir / ACCOUNT_FILE).read_text() == "{oops"


def test_a_6pct_drop_trips_the_drawdown_breaker(world):
    a = world.runner.account
    a.modes.mode = Mode.RUNNING
    totals = {**world.ex.bal, "USDT": world.ex.bal["USDT"] - Dec("61")}
    st = a.state(world.runner.ledger, totals, Dec("10000"), {"BTCUSDT": Dec("80000"), "ETHUSDT": Dec("2500")},
                 {}, T0)
    assert st.mode is Mode.ENTRIES_BLOCKED and "REJ_DRAWDOWN_BREAKER" in a.modes.active_breaches


# ---------------------------------------------------------------- exits
def opened(w, qty="0.0012"):
    t = w.runner.ledger.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", T0, "H2b")
    w.ex._put(t.ids["stop"], type="STOP_LOSS", orig_qty=Dec(qty), stop_price=Dec("78000"))
    w.ex._put(t.ids["take-profit"], type="LIMIT_MAKER", orig_qty=Dec(qty), price=Dec("86000"))
    w.ex.bal["BTC"] += Dec(qty)
    w.runner.ledger.mark(t.intent_id, OPEN, T0, received_qty=Dec(qty),
                         prices={"entry_price": Dec("80000"), "stop_price": Dec("78000"),
                                 "take_profit_price": Dec("86000")})
    return t


def test_closing_cancels_the_protection_then_sells_what_is_left(world):
    t = opened(world)
    r = exits.close_trade(t, world.ex, RULES)
    assert r.closed and r.left_qty == 0 and r.sale.executed_qty == Dec("0.0012")
    assert [k for k, _ in world.ex.sent] == ["cancel", "sell"]


def test_coins_a_protective_order_sold_are_not_sold_again(world):
    t = opened(world)
    world.ex.fill_stop(t.ids["stop"], "78000")
    r = exits.close_trade(t, world.ex, RULES)
    assert r.closed and r.sale is None and r.sold_by_protection == Dec("0.0012")
    assert ("sell", t.ids["exit"]) not in world.ex.sent


def test_a_sale_sent_before_is_looked_up_never_sent_twice(world):
    t = opened(world)
    world.ex._put(t.ids["exit"], type="MARKET", status="FILLED", orig_qty=Dec("0.0012"),
                  executed_qty=Dec("0.0012"))
    assert exits.close_trade(t, world.ex, RULES).closed
    world.ex.orders[t.ids["exit"]] = OrderState(**{**world.ex.orders[t.ids["exit"]].__dict__, "status": "EXPIRED"})
    r = exits.close_trade(t, world.ex, RULES)
    assert not r.closed and "never sent twice" in r.problem
    assert ("sell", t.ids["exit"]) not in world.ex.sent


def test_a_failed_sale_is_reported_not_closed(world):
    t = opened(world)
    world.ex.sale_error = OrderRejected("Binance refused the order: MIN_NOTIONAL")
    r = exits.close_trade(t, world.ex, RULES)
    assert not r.closed and "failed" in r.problem


def test_less_than_the_minimum_is_left_as_dust(world):
    t = opened(world, qty="0.000005")
    r = exits.close_trade(t, world.ex, RULES)
    assert r.closed and r.left_qty == Dec("0.000005") and r.sale is None


# ---------------------------------------------------------------- the runner
def test_the_runner_trades_live_only_with_the_live_switch(world):
    world.ex.environment = "live"                              # no authorization on this trader
    with pytest.raises(RunnerError, match="live switch"):
        TestnetRunner(world.dir, world.cfg, world.ex, world.runner.exchange, world.runner.signals, [], {},
                      world.runner.ledger, world.runner.account)
    world.ex.environment = "paper"
    with pytest.raises(RunnerError, match="not 'paper'"):
        TestnetRunner(world.dir, world.cfg, world.ex, world.runner.exchange, world.runner.signals, [], {},
                      world.runner.ledger, world.runner.account)


def test_entries_start_blocked_and_nothing_is_sent(world):
    world.s.ideas = [intent()]
    world.runner.tick()
    assert not world.ex.sent and not world.runner.ledger.trades
    assert events(world.dir, "entry_blocked")[0]["mode"] == "ENTRIES_BLOCKED"


def test_after_resume_an_idea_becomes_a_protected_trade_in_the_ledger(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    assert t.status == OPEN and t.strategy == "H2b" and t.stop_price == Dec("78000.00")
    assert t.entry_price == Dec("80000") and t.expires_at == pytest.approx(world.clock() + 720 * 3600)
    assert world.ex.sent == [("opoco", "BTCUSDT")]
    assert events(world.dir, "entry_protected")
    assert json.loads((world.dir / CLOCK_FILE).read_text())["H2b"] > 0
    world.runner.tick()                                        # same candle: no second decision
    assert world.s.calls == 2 and world.runner.ledger.trades[t.intent_id].status == OPEN


def test_the_ledger_has_the_trade_before_its_order_is_sent(world):
    resume(world)
    next_candle(world)
    seen = {}
    real = world.ex.place_protected_entry

    def spy(req, rules, fee_rate=Dec("0.001")):
        seen["ledger"] = json.loads((world.dir / "ledger.json").read_text())
        return real(req, rules, fee_rate)
    world.ex.place_protected_entry = spy
    world.s.ideas = [intent()]
    world.runner.tick()
    assert [t["status"] for t in seen["ledger"]["trades"]] == [PENDING]


def test_an_idea_the_risk_engine_rejects_sends_nothing(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [TradeIntent("BTCUSDT", "BUY", Dec("80080"), Dec("78000"), Dec("80100"), "H2b", 720, "x")]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    assert t.status == NOT_OPENED and not world.ex.sent and events(world.dir, "entry_rejected")


def test_a_strategy_exit_sells_its_own_trade(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    next_candle(world)
    world.s.exits = [Exit("BTCUSDT", "2 of the 14/28/56-day returns turned negative")]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    assert t.status == CLOSED and "strategy exit" in t.note
    assert [k for k, _ in world.ex.sent] == ["opoco", "cancel", "sell"]
    assert world.ex.bal["BTC"] == Dec("1") and events(world.dir, "trade_closed")


def test_the_time_stop_sells(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent(hours=1)]
    world.runner.tick()
    world.clock.advance(3601)
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    assert t.status == CLOSED and t.note == "time stop"


def test_a_stop_filled_on_binance_is_recorded_by_reconciliation(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    world.ex.fill_stop(t.ids["stop"], "78000")
    world.runner.tick()
    assert t.status == CLOSED and "stop-loss" in t.note and not any(k == "sell" for k, _ in world.ex.sent)
    st = world.runner._state(world.clock(), True)
    assert st.equity < 1000 and st.positions == ()


def test_coins_found_unprotected_are_sold_and_entries_blocked(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    for k in ("stop", "take-profit"):                          # someone cancelled the protection by hand
        o = world.ex.orders[t.ids[k]]
        world.ex.orders[t.ids[k]] = OrderState(**{**o.__dict__, "status": "CANCELED"})
    world.runner.tick()
    assert t.status == CLOSED and ("sell", t.ids["exit"]) in world.ex.sent
    assert world.runner.account.modes.mode is Mode.ENTRIES_BLOCKED and events(world.dir, "unprotected")
    world.runner.tick()                                        # clean again: the fault goes, still blocked
    assert "UNPROTECTED_POSITION" not in world.runner.account.modes.active_breaches
    assert world.runner.account.modes.mode is Mode.ENTRIES_BLOCKED and events(world.dir, "fault_cleared")
    resume(world)


def test_a_mismatch_blocks_until_it_is_gone_and_an_operator_resumes(world):
    resume(world)
    world.ex._put("web_1", side="BUY", orig_qty=Dec("0.1"))   # an order that is not the bot's
    world.runner.tick()
    assert "RECONCILIATION_MISMATCH" in world.runner.account.modes.active_breaches
    request(world.dir, "resume")
    world.runner.tick()
    assert events(world.dir, "resume_refused") and world.runner.account.modes.mode is Mode.ENTRIES_BLOCKED
    del world.ex.orders["web_1"]
    world.runner.tick()
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    assert world.ex.sent == [("opoco", "BTCUSDT")]


def test_an_exchange_error_pauses_entries_for_that_round_only(world):
    resume(world)
    next_candle(world)
    world.ex.fail_reads = True
    world.s.ideas = [intent()]
    world.runner.tick()
    assert not world.ex.sent and events(world.dir, "entries_paused")
    assert world.runner.account.modes.mode is Mode.RUNNING                 # no operator needed
    world.ex.fail_reads = False
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    assert world.ex.sent == [("opoco", "BTCUSDT")] and events(world.dir, "entries_unpaused")


def test_an_operator_can_block_entries(world):
    resume(world)
    request(world.dir, "block", reason="checking the account by hand")
    world.runner.tick()
    assert world.runner.account.modes.mode is Mode.ENTRIES_BLOCKED


def test_the_runner_never_imports_the_paper_account_or_takes_a_decision():
    import ast
    tree = ast.parse((SRC / "execution" / "runner.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m and m.startswith(("bot.paper", "bot.backtest", "bot.news", "bot.llm")) for m in mods)
    assert "evaluate(" not in (SRC / "execution" / "runner.py").read_text()   # only entry.submit runs it
    assert "place_protected_entry" not in (SRC / "execution" / "runner.py").read_text()


# ---------------------------------------------------------------- commands
def test_testnet_start_and_status(tmp_path, capsys):
    ex = FakeBinance()
    book = Ledger.load(tmp_path, "testnet")
    book.set_baseline({"BTC": Dec("1"), "ETH": Dec("1")}, "Sultan", "testnet account as given", T0)
    book.save()
    args = argparse.Namespace(config="config/default.yaml", state_dir=str(tmp_path), operator="Sultan", capital="1000")
    out = io.StringIO()
    code = cli._testnet_start(args, make_trader=lambda **k: ex, make_reader=lambda **k: FakeMarket(),
                              load_creds=lambda: object(), out=out)
    assert code == 0 and "STARTED by Sultan" in out.getvalue() and "1,000.00 USDT" in out.getvalue()
    code = cli._testnet_start(args, make_trader=lambda **k: ex, make_reader=lambda **k: FakeMarket(),
                              load_creds=lambda: object(), out=io.StringIO())
    assert code == 2 and "started once" in capsys.readouterr().err
    out = io.StringIO()
    assert cli._testnet_status(argparse.Namespace(state_dir=str(tmp_path)), out=out) == 0
    assert "Mode: ENTRIES_BLOCKED" in out.getvalue() and "0 open" in out.getvalue()


def test_no_round_trip_once_the_runner_account_exists(world, capsys):
    args = argparse.Namespace(config="config/default.yaml", symbol="BTCUSDT", stop_pct="1.5", equity="1000",
                              hold=0, journal_dir=str(world.dir))
    assert cli._testnet_roundtrip(args, load_creds=lambda: object()) == 2
    assert "runner's account is started" in capsys.readouterr().err


def test_an_order_still_unsettled_pauses_entries(world):
    resume(world)
    world.runner.ledger.add_pending(__import__("uuid").uuid4(), "BTCUSDT", "BTC", world.clock())  # sent, not found yet
    next_candle(world)
    world.runner.ledger.trades[next(iter(world.runner.ledger.trades))].created_at = world.clock() - 10
    world.s.ideas = [intent()]
    world.runner.tick()
    assert not world.ex.sent and events(world.dir, "entry_blocked") and events(world.dir, "entries_paused")


def test_the_risk_engine_sees_open_trades_and_never_adds_to_one(world):
    resume(world)
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    st = world.runner._state(world.clock(), True)
    (p,) = st.positions
    assert p.symbol == "BTCUSDT" and p.stop_price == Dec("78000.00") and p.corr_group == "majors"
    next_candle(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    assert world.ex.sent == [("opoco", "BTCUSDT")]
    assert events(world.dir, "entry_rejected")[0]["code"] == "REJ_NO_ADDING"


def test_nothing_is_sold_while_a_protective_order_is_still_open(world):
    t = opened(world)
    world.ex.cancel_list = lambda symbol, list_cid: True          # Binance says yes, nothing changes
    r = exits.close_trade(t, world.ex, RULES)
    assert not r.closed and "still open" in r.problem and not world.ex.sent


# ---------------------------------------------------------------- PR-1: an unknown entry blocks every later entry
def eth_intent():
    return TradeIntent("ETHUSDT", "BUY", Dec("2502.5"), Dec("2400"), Dec("2810"), "H2b", 720, "2 of 3 returns negative")


def unknown_entries(w):
    """Every order list's outcome is unknown (no answer, still not found after the trader's own lookups)."""
    from bot.exchange.trading import OrderStatusUnknown
    calls = []

    def unknown(req, rules, fee_rate=Dec("0.001")):
        calls.append(req.symbol)
        w.ex.sent.append(("opoco", req.symbol))
        raise OrderStatusUnknown(req.symbol, req.entry_client_id, "NetworkError: read timed out")
    w.ex.place_protected_entry = unknown
    return calls


def test_an_unknown_entry_blocks_every_later_idea_in_the_same_round(world):
    resume(world)
    next_candle(world)
    calls = unknown_entries(world)
    world.s.ideas = [intent(), eth_intent()]
    world.runner.tick()
    assert calls == ["BTCUSDT"]                                           # the ETH idea was never sent
    assert [t.status for t in world.runner.ledger.trades.values()] == [PENDING]
    assert events(world.dir, "entry_unknown")[0]["symbol"] == "BTCUSDT"
    (b,) = [e for e in events(world.dir, "entry_blocked") if e["symbol"] == "ETHUSDT"]
    assert "outcome is not known" in b["reason"] and b["pending"]


def test_a_failed_cancel_inside_the_entry_blocks_the_same_way(world):
    resume(world)
    next_candle(world)

    def stuck(req, rules, fee_rate=Dec("0.001")):
        world.ex.sent.append(("opoco", req.symbol))
        return world.ex._put(req.entry_client_id, side="BUY", status="NEW", orig_qty=req.qty)   # neither filled nor ended

    def no_answer(symbol, list_cid):
        raise ExchangeError(f"cancel order list {list_cid}: no answer")
    world.ex.place_protected_entry, world.ex.cancel_list = stuck, no_answer
    world.s.ideas = [intent(), eth_intent()]
    world.runner.tick()
    assert world.ex.sent == [("opoco", "BTCUSDT")]
    assert [t.status for t in world.runner.ledger.trades.values()] == [PENDING]
    assert any(e["symbol"] == "ETHUSDT" for e in events(world.dir, "entry_blocked"))


def test_the_block_lasts_until_reconciliation_settles_the_trade(world):
    resume(world)
    next_candle(world)
    unknown_entries(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    world.ex.place_protected_entry = FakeBinance.place_protected_entry.__get__(world.ex)   # Binance answers again
    next_candle(world)                                         # 4 hours later: not found, and nothing else either
    world.s.ideas = [eth_intent()]
    world.runner.tick()
    assert world.runner.ledger.trades[t.intent_id].status == NOT_OPENED
    assert "never placed" in world.runner.ledger.trades[t.intent_id].note
    assert ("opoco", "ETHUSDT") in world.ex.sent                # settled first, so the next idea may go


def test_still_pending_while_young_and_uncertain_while_any_record_disagrees(world):
    from bot.exchange.trading import TradeRecord
    resume(world)
    next_candle(world)
    unknown_entries(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    world.clock.advance(60)                                    # young: unsettled, the next idea is blocked
    world.runner.tick()
    assert world.runner.ledger.trades[t.intent_id].status == PENDING and events(world.dir, "entries_paused")
    world.ex.history = [TradeRecord(7, True, Dec("0.0012"), int((t.created_at + 1) * 1000))]   # a buy did happen
    next_candle(world)
    world.s.ideas = [eth_intent()]
    world.runner.tick()
    assert world.runner.ledger.trades[t.intent_id].status == PENDING
    assert "uncertain" in events(world.dir, "reconciliation_mismatch")[-1]["detail"]
    assert "RECONCILIATION_MISMATCH" in world.runner.account.modes.active_breaches
    assert ("opoco", "ETHUSDT") not in world.ex.sent


def test_an_entry_found_filled_later_becomes_an_open_trade_the_engine_counts(world):
    resume(world)
    next_candle(world)
    unknown_entries(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    world.ex.bal["USDT"] -= Dec("0.0012") * Dec("80000")       # Binance did execute it, protection and all
    world.ex.bal["BTC"] += Dec("0.0012")
    world.ex._put(t.ids["entry"], side="BUY", type="LIMIT", status="FILLED", orig_qty=Dec("0.0012"),
                  executed_qty=Dec("0.0012"), quote_qty=Dec("96"))
    world.ex._put(t.ids["stop"], type="STOP_LOSS", orig_qty=Dec("0.0012"), stop_price=Dec("78000"))
    world.ex._put(t.ids["take-profit"], type="LIMIT_MAKER", orig_qty=Dec("0.0012"), price=Dec("86240"))
    world.clock.advance(60)
    world.runner.tick()
    assert world.runner.ledger.trades[t.intent_id].status == OPEN
    (p,) = world.runner._state(world.clock(), True).positions
    assert p.symbol == "BTCUSDT" and p.qty == Dec("0.0012")


def test_the_block_survives_a_restart(world):
    resume(world)
    next_candle(world)
    unknown_entries(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    world.runner.account.save()
    again = TestnetRunner(world.dir, world.cfg, world.ex, world.runner.exchange, world.runner.signals, [world.s],
                          {"BTCUSDT": "majors", "ETHUSDT": "majors"}, Ledger.load(world.dir, "testnet"),
                          TradingAccount.load(world.dir, world.cfg, world.clock()), clock=world.clock)
    assert [t.status for t in again.ledger.trades.values()] == [PENDING]
    request(world.dir, "resume", reason="operator resumes after the restart")
    world.clock.advance(60)
    again.tick()
    assert again.account.modes.mode is Mode.RUNNING              # the operator resumed ...
    sent = list(world.ex.sent)
    again._enter(eth_intent(), world.clock(), entries_ok=True)   # even with reconciliation clean, the gate holds
    assert world.ex.sent == sent
    assert events(world.dir, "entry_blocked")[-1]["symbol"] == "ETHUSDT"
    assert "outcome is not known" in events(world.dir, "entry_blocked")[-1]["reason"]


def test_the_pending_check_comes_before_a_new_trade_is_written():
    import inspect
    src = inspect.getsource(TestnetRunner._enter)
    gate, write = src.index("t.status == PENDING"), src.index("add_pending(")
    assert gate < write and "return" in src[gate:write]


# ---------------------------------------------------------------- PR-1b: unreadable replies, fills that are not the bot's
def test_the_runner_survives_an_unreadable_reply_and_recovers(world):
    from bot.exchange.trading import MalformedResponse
    from bot.execution.ledger import LEDGER
    resume(world)
    good = world.ex.open_orders

    def garbled(symbol=None):
        raise MalformedResponse("openOrders: Binance's reply is not a list")
    world.ex.open_orders = garbled
    before = (world.dir / LEDGER).read_bytes()
    for _ in range(3):                                                   # three rounds: no crash
        world.clock.advance(60)
        world.runner.tick()
    assert (world.dir / LEDGER).read_bytes() == before
    assert len(events(world.dir, "exchange_data_error")) == 1           # said once, not every round
    assert events(world.dir, "entries_paused")
    world.ex.open_orders = good
    world.clock.advance(60)
    world.runner.tick()
    assert events(world.dir, "entries_unpaused") and world.runner.account.modes.mode is Mode.RUNNING


def test_a_restart_settles_a_trade_whose_only_fill_was_a_manual_buy(world):
    from bot.exchange.trading import TradeRecord
    from bot.execution import reconcile as rc
    resume(world)
    next_candle(world)
    unknown_entries(world)
    world.s.ideas = [intent()]
    world.runner.tick()
    (t,) = world.runner.ledger.trades.values()
    world.ex._put("web_manual", side="BUY", type="MARKET", status="FILLED", orig_qty=Dec("0.001"),
                  executed_qty=Dec("0.001"), order_id=900)
    world.ex.bal["BTC"] += Dec("0.001")
    world.ex.history = [TradeRecord(900, True, Dec("0.001"), int((t.created_at + 5) * 1000), Dec(0), "USDT")]
    world.runner.account.save()
    world.clock.advance(rc.SETTLE_S + 60)
    again = TestnetRunner(world.dir, world.cfg, world.ex, world.runner.exchange, world.runner.signals, [world.s],
                          {"BTCUSDT": "majors", "ETHUSDT": "majors"}, Ledger.load(world.dir, "testnet"),
                          TradingAccount.load(world.dir, world.cfg, world.clock()), clock=world.clock)
    again.tick()
    assert again.ledger.trades[t.intent_id].status == NOT_OPENED          # settled: the fill was not the bot's
    assert Ledger.load(world.dir, "testnet").trades[t.intent_id].status == NOT_OPENED
    assert any("BTC: Binance holds" in e["detail"] for e in events(world.dir, "reconciliation_mismatch"))
