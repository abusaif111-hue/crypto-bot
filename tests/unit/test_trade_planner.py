"""DailyTradePlanner (`bot.trade_planner`, `bot plan`): full plans for the 20 USDT pilot, and whether any order
can pass at all. Made-up candles and illustrative exchange rules; nothing here reads Binance or trades."""
import argparse
import io
import json
from dataclasses import replace
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot import trade_planner as tp
from bot.core.config import AppConfig
from bot.daily_signal import account
from bot.exchange.binance import ExchangeError
from bot.exchange.credentials import CredentialsError
from bot.exchange.filters import parse_symbol
from bot.portfolio.accounting import Balance
from bot.strategies import daily_pullback as dp
from tests.conftest import exchange_info_symbol
from tests.fakes import FakeMarket
from tests.unit.test_daily_pullback import H4, Data, _imports, setup

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
BTC = parse_symbol(exchange_info_symbol("BTCUSDT"))


class Ask(Data):
    """Data whose best ask is moved by `bump` from the last close."""

    def __init__(self, by_symbol, bump):
        super().__init__(by_symbol)
        self.bump = Dec(str(bump))

    def snapshot(self, symbol):
        s = super().snapshot(symbol)
        return replace(s, best_ask=s.best_ask * (1 + self.bump), best_bid=s.best_bid * (1 + self.bump))


def big(cs, now, data=None, equity=1000, cfg=None):
    """A plan on a hypothetical account bigger than the pilot, to see a complete TRADE plan."""
    return tp.plan_coin(dp.DailyPullback(["BTCUSDT"]), data or Data({"BTCUSDT": cs}), "BTCUSDT", cfg or AppConfig(),
                        account(Dec(equity), Dec(0), Dec("0.001")), now)


# ---------------------------------------------------------------- the smallest order and what it needs
def test_smallest_order_meets_min_notional_on_the_step_size():
    assert tp.smallest_order(BTC, Dec(80000)) == Dec("0.00007")          # 0.0000625 rounded UP to the step
    assert tp.smallest_order(BTC, Dec(50000)) == Dec("0.0001")           # exactly 5 USDT
    q = tp.smallest_order(BTC, Dec("81234.56"))
    assert q * Dec("81234.56") >= 5 > (q - BTC.step_size) * Dec("81234.56")


@pytest.mark.parametrize("price", ["2500", "4000", "65000", "80000", "123456.78"])
def test_twenty_usdt_can_never_hold_the_smallest_order_under_the_one_coin_limit(price):
    # R-07: one coin at most 25% of equity, valued at the entry plus slippage: 25% of 20 USDT is under 5 USDT.
    entry = Dec(price)
    f = tp.feasibility("BTCUSDT", entry, entry * Dec("0.97"), BTC, AppConfig(), Dec("0.001"), Dec(20))
    assert f.needed["coin"] > 20 and not f.viable


def test_loss_per_unit_is_the_risk_engines_own():
    cs, now = setup()
    p = big(cs, now)
    assert p.trade, p.reason
    d = p.decision.details                                              # the engine's prices, rounded to the tick
    assert tp.loss_per_unit(Dec(d["entry"]), Dec(d["stop"]), "BTCUSDT", AppConfig(), Dec("0.001")) == Dec(
        d["loss_per_unit"])
    f = tp.feasibility("BTCUSDT", p.verdict.entry, p.verdict.stop, BTC, AppConfig(), Dec("0.001"), Dec(1000))
    assert (f.entry, f.stop) == (Dec(d["entry"]), Dec(d["stop"]))      # feasibility rounds the same way


def test_needed_equity_per_limit():
    f = tp.feasibility("BTCUSDT", Dec(80000), Dec(78800), BTC, AppConfig(), Dec("0.001"), Dec(20))
    assert f.qty == Dec("0.00007") and f.notional == Dec("5.6")
    assert f.needed["risk"] == f.worst_loss / Dec("0.0025")              # 0.25% configured
    assert f.needed["risk_max"] == f.worst_loss / Dec("0.005")           # Sultan's 0.5% maximum, shown only
    assert f.binding == "risk" and f.needed_equity == f.needed["risk"] and not f.viable
    assert "risk_max" not in (f.binding,)


# ---------------------------------------------------------------- the pilot account
def test_the_pilot_is_capped_at_twenty_usdt():
    cs, now = setup()
    for bad in (Dec(0), Dec("20.01"), Dec(1000)):
        with pytest.raises(ValueError):
            tp.plan(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), bad, now)


def test_a_long_on_twenty_usdt_is_no_trade_with_the_engines_reason_and_the_constraint():
    cs, now = setup()
    [p] = tp.plan(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(20), now)
    assert p.verdict.long and not p.trade
    assert "risk engine says no (REJ_BELOW_EXCHANGE_MIN)" in p.reason
    assert p.feasibility is not None and not p.feasibility.viable
    text = tp.render([p], Dec(20), Dec(0), now, AppConfig())
    assert "BTCUSDT: NO TRADE" in text and "NOT feasible" in text and "not raised to fit" in text


def test_no_signal_still_reports_the_smallest_order_with_d1s_own_stop():
    cs, now = setup()
    cs = cs[:-1]                                                        # no resume candle: no signal
    [p] = tp.plan(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(20), now + H4 / 1000)
    assert not p.trade and p.feasibility is not None and p.feasibility.stop < p.feasibility.entry


# ---------------------------------------------------------------- a complete plan
def test_a_trade_plan_has_every_field_and_the_engines_numbers():
    cs, now = setup()
    p = big(cs, now)
    assert p.trade
    d, v = p.decision, p.verdict
    assert d.accepted and d.planned_risk <= Dec("2.5") and d.reward_risk >= Dec("1.5")
    assert p.fees > 0 and p.slippage > 0 and p.expires_at == (v.candle_ms + 2 * H4) / 1000 > now
    assert len(p.invalidation) == 5
    text = tp.render([p], Dec(1000), Dec(0), now, AppConfig())
    for part in ("TRADE PLAN", "BUY LIMIT FOK", "Stop-loss", "Take-profit", "Size", "Fees (est.)", "Slippage",
                 "Max loss", "Net reward/risk", "Expires", "Invalid if", "Reason", "READ-ONLY"):
        assert part in text, part


def test_no_chasing_when_the_price_is_above_the_entry_limit():
    cs, now = setup()
    p = big(cs, now, Ask({"BTCUSDT": cs}, "0.002"))                      # entry limit is the close + 0.10%
    assert not p.trade and "no chasing" in p.reason and p.decision is None


def test_an_expired_plan_is_no_trade():
    cs, now = setup()
    v = dp.DailyPullback(["BTCUSDT"]).signal(cs, now)
    late = (v.candle_ms + 2 * H4) / 1000

    class Frozen(dp.DailyPullback):
        def signal(self, cs, now):
            return v
    p = tp.plan_coin(Frozen(["BTCUSDT"]), Data({"BTCUSDT": cs}), "BTCUSDT", AppConfig(),
                     account(Dec(1000), Dec(0), Dec("0.001")), late)
    assert not p.trade and "expired" in p.reason


def test_market_data_failure_is_no_trade():
    cs, now = setup()
    [p] = tp.plan(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}, fail={"BTCUSDT"}), AppConfig(), Dec(20), now)
    assert not p.trade and "market data unavailable" in p.reason


def test_the_daily_loss_limit_blocks_a_plan():
    cs, now = setup()
    p = tp.plan_coin(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), "BTCUSDT", AppConfig(),
                     account(Dec(1000), Dec(15), Dec("0.001")), now)          # R-02: 1.5% already lost
    assert not p.trade and "risk engine says no" in p.reason


def test_record_appends_one_line_per_coin(tmp_path):
    cs, now = setup()
    plans = [big(cs, now), *tp.plan(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(20), now)]
    path = tp.record(tmp_path / "log", plans, Dec(20))
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["plan"] for r in rows] == ["TRADE", "NO TRADE"]
    assert {"entry", "stop", "take_profit", "qty", "max_loss", "fees", "slippage", "expires_at"} <= set(rows[0])
    assert rows[1]["feasibility"]["viable"] is False
    assert list((tmp_path / "log").iterdir()) == [path]


# ---------------------------------------------------------------- CLI
def args(tmp_path, **kw):
    a = dict(config="config/default.yaml", equity="20", loss_today="0", fee="0.001", from_account=False,
             log_dir=str(tmp_path / "plans"), no_log=False)
    a.update(kw)
    return argparse.Namespace(**a)


class Account(FakeMarket):
    def __init__(self, free="13.5", fail=None):
        super().__init__()
        self.free, self.fail = Dec(free), fail

    def balances(self):
        if self.fail:
            raise self.fail
        return [Balance("USDT", self.free, Dec(0)), Balance("BTC", Dec("0.001"), Dec(0))]

    def taker_fee(self, symbol):
        return Dec("0.00075")


def test_cli_prints_and_logs(tmp_path):
    out = io.StringIO()
    assert cli._plan(args(tmp_path), make_exchange=lambda environment: FakeMarket(), out=out) == 0
    assert "DailyTradePlanner" in out.getvalue() and "READ-ONLY" in out.getvalue()
    assert (tmp_path / "plans" / tp.LOG_FILE).exists()
    assert cli._plan(args(tmp_path / "x", no_log=True), make_exchange=lambda environment: FakeMarket(),
                     out=io.StringIO()) == 0
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("kw", [{"equity": "0"}, {"equity": "20.01"}, {"equity": "1000"}, {"equity": "abc"},
                                {"loss_today": "-1"}, {"fee": "1"}])
def test_cli_refuses_bad_inputs_and_more_than_the_pilot(tmp_path, kw):
    assert cli._plan(args(tmp_path, **kw), make_exchange=lambda environment: FakeMarket(), out=io.StringIO()) == 2


def test_cli_from_account_uses_the_real_free_usdt_capped_at_the_pilot(tmp_path):
    seen = {}

    def make(environment, credentials=None):
        seen["env"], seen["creds"] = environment, credentials
        return Account("13.5")
    out = io.StringIO()
    assert cli._plan(args(tmp_path, from_account=True), make_exchange=make, load_creds=lambda: "KEY", out=out) == 0
    assert seen == {"env": "live", "creds": "KEY"}
    assert "13.5 USDT free" in out.getvalue() and "the pilot uses 13.5 USDT" in out.getvalue()
    rows = [json.loads(x) for x in (tmp_path / "plans" / tp.LOG_FILE).read_text().splitlines()]
    assert {r["equity"] for r in rows} == {"13.5"}

    out = io.StringIO()
    assert cli._plan(args(tmp_path, from_account=True), make_exchange=lambda environment, credentials: Account("500"),
                     load_creds=lambda: "KEY", out=out) == 0
    assert "the pilot uses 20 USDT" in out.getvalue()                   # never more than the allocation


def test_cli_from_account_reports_a_missing_key_or_an_empty_account(tmp_path):
    def no_key():
        raise CredentialsError("BINANCE_API_KEY is not set")
    assert cli._plan(args(tmp_path, from_account=True), make_exchange=lambda environment, credentials: Account(),
                     load_creds=no_key, out=io.StringIO()) == 4
    assert cli._plan(args(tmp_path, from_account=True),
                     make_exchange=lambda environment, credentials: Account(fail=ExchangeError("down")),
                     load_creds=lambda: "KEY", out=io.StringIO()) == 4
    assert cli._plan(args(tmp_path, from_account=True), make_exchange=lambda environment, credentials: Account("0"),
                     load_creds=lambda: "KEY", out=io.StringIO()) == 3


def test_cli_parser_has_the_command():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--help"])


# ---------------------------------------------------------------- isolation
def test_the_planner_cannot_reach_orders_credentials_or_h2b():
    mods = _imports(SRC / "trade_planner.py")
    for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.credentials", "bot.exchange.binance",
                "bot.forward", "bot.paper", "bot.strategies.h2b_blend"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad


def test_the_planner_is_not_wired_into_any_runner():
    users = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if "bot.trade_planner" in _imports(p)]
    assert users == []                                   # cli.py imports it as "from bot import trade_planner"
    text_users = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if "trade_planner" in p.read_text()
                  and p.name != "trade_planner.py"]
    assert text_users == ["cli.py"]


def test_the_planner_never_changes_a_risk_setting():
    src = (SRC / "trade_planner.py").read_text()
    assert "AppConfig(" not in src and "replace(" not in src and "risk_per_trade_pct =" not in src


# ---------------------------------------------------------------- the watchlist (setups that have not triggered)
class Day:
    def __init__(self, close):
        self.close = Dec(str(close))


def test_the_daily_close_needed_is_exactly_d1s_threshold():
    s = dp.DailyPullback(["BTCUSDT"])
    falling = [100 * (1 - 0.001) ** i for i in range(200)]                # EMA falling: needs a big close
    for closes in (falling, [100 * (1 + 0.0005) ** i for i in range(200)][:-1] + [90.0]):
        c = tp.trend_close_needed(s, [Dec(str(x)) for x in closes])
        days = [Day(x) for x in closes]
        assert s.uptrend(days + [Day(c + Dec("0.0001"))])[0]
        assert not s.uptrend(days + [Day(c - Dec("0.0001"))])[0]


def watch_at(cs, now, equity=20, data=None, symbol="BTCUSDT"):
    return tp.watch_coin(dp.DailyPullback([symbol]), data or Data({symbol: cs}), symbol, AppConfig(),
                         account(Dec(equity), Dec(0), Dec("0.001")), now)


def test_a_live_signal_on_a_big_enough_account_is_ready():
    cs, now = setup()
    w = watch_at(cs, now, equity=1000)
    assert w.status == tp.READY and w.pilot.accepted and w.entry > w.stop


def test_without_the_trigger_it_watches_for_the_next_4h_close_above_the_last_high():
    cs, now = setup()
    cs = cs[:-1]
    w = watch_at(cs, now + H4 / 1000, equity=1000)
    assert w.status == tp.WATCHING, w.reason
    assert any("50-day EMA" in m for m in w.met) and any(f"4h close above {cs[-1].high}" in n for n in w.needs)
    assert w.entry > cs[-1].high and w.stop < w.entry < w.target and w.check.accepted
    assert w.watch_until > now


def test_on_the_pilot_the_same_setup_is_not_tradeable_and_says_why():
    cs, now = setup()
    w = watch_at(cs[:-1], now + H4 / 1000, equity=20)
    assert w.status == tp.NOT_TRADEABLE and "not on the pilot account" in w.reason
    assert "REJ_BELOW_EXCHANGE_MIN" in w.reason and not w.feasibility.viable


def test_a_setup_the_engine_refuses_at_any_size_is_not_tradeable():
    cs, now = setup(half_range=0.006)                                     # stop too tight for 1.5 after costs
    w = watch_at(cs[:-1], now + H4 / 1000, equity=1000)
    assert w.status == tp.NOT_TRADEABLE and "at any size (REJ_REWARD_RISK)" in w.reason


def test_a_downtrend_lists_the_daily_close_it_needs():
    from tests.unit.test_daily_pullback import N, candles
    cs = candles([80_000 * (1 - 0.0005) ** i for i in range(N)], 0.009)
    w = watch_at(cs, (cs[-1].close_time + 1) / 1000, equity=1000)
    assert w.status in (tp.WATCHING, tp.NOT_TRADEABLE) and any("a daily close above" in n for n in w.needs)


def test_the_watchlist_is_capped_at_the_pilot_and_renders_a_table():
    cs, now = setup()
    with pytest.raises(ValueError):
        tp.watch(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(21), now)
    ws = tp.watch(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs[:-1]}), AppConfig(), Dec(20), now + H4 / 1000)
    text = tp.render_watch(ws, Dec(20), now)
    for part in ("Status", "Entry", "Stop", "Target", "Max loss", "Watch until", "NOT TRADEABLE", "Needs:",
                 "Projections at the current levels, not orders"):
        assert part in text, part


def test_cli_watch(tmp_path):
    out = io.StringIO()
    assert cli._plan(args(tmp_path, watch=True), make_exchange=lambda environment: FakeMarket(), out=out) == 0
    assert "watchlist" in out.getvalue() and "NO DATA" in out.getvalue()
