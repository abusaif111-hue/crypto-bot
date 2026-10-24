"""Module 5f: live mode. Nothing reaches the real exchange's order endpoints without every layer of
Sultan's live switch: the config (environment live, live_trading true), the typed confirmation, a
trading key that can trade Spot and nothing else, and a capped account within the ceiling."""
import argparse
import io
import json
import subprocess
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import alerts, cli
from bot.core import config as config_mod
from bot.core.config import (LIVE_CAPITAL_CEILING_USDT, LIVE_CONFIRM_ENV, LIVE_CONFIRM_PHRASE, AppConfig,
                             LiveTradingNotConfirmed)
from bot.exchange.binance import ExchangeError, trading_key_problems
from bot.exchange.trading import BinanceTrader, LiveAuthorization, authorize_live
from bot.execution.account import ACCOUNT_FILE, AccountError, TradingAccount
from bot.execution.ledger import Ledger
from bot.execution.runner import RunnerError, TestnetRunner
from bot.market.data import MarketDataService
from tests.fakes import FakeMarket
from tests.unit.test_testnet_runner import T0, FakeBinance, Idea
from tests.unit.test_trading_adapter import RULES, FakeClient, buy, filled

ROOT = Path(__file__).resolve().parents[2]
LIVE_CFG = AppConfig(environment="live", live_trading=True)
SWITCH = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE}
SAFE_KEY = {"ipRestrict": True, "enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False,
            "enableInternalTransfer": False, "permitsUniversalTransfer": False, "enableMargin": False,
            "enableFutures": False, "enableVanillaOptions": False}


# ---------------------------------------------------------------- the switch
def test_only_authorize_live_makes_an_authorization_and_only_with_both_halves_of_the_switch():
    with pytest.raises(ExchangeError, match="only by authorize_live"):
        LiveAuthorization(object())
    with pytest.raises(LiveTradingNotConfirmed, match="disabled in the config"):
        authorize_live(AppConfig(), SWITCH)
    with pytest.raises(LiveTradingNotConfirmed, match="disabled in the config"):
        authorize_live(AppConfig(environment="live"), SWITCH)
    with pytest.raises(LiveTradingNotConfirmed, match="confirmation phrase"):
        authorize_live(LIVE_CFG, {})
    with pytest.raises(LiveTradingNotConfirmed, match="confirmation phrase"):
        authorize_live(LIVE_CFG, {LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE.lower()})
    assert isinstance(authorize_live(LIVE_CFG, SWITCH), LiveAuthorization)


def test_without_the_switch_a_live_trader_can_read_and_test_but_never_place_or_cancel():
    c = FakeClient().script(("POST", "/api/v3/order/test"), {}).script(
        ("GET", "/api/v3/account"), {"canTrade": True, "canWithdraw": True, "accountType": "SPOT", "permissions": [],
                                     "balances": []})
    t = BinanceTrader(environment="live", client=c)
    t.test_order(buy(), RULES)
    t.account()
    for call in (lambda: t.place(buy(), RULES), lambda: t.cancel_list("BTCUSDT", "sbl0" + "a" * 32),
                 lambda: t.cancel("BTCUSDT", "sbe0" + "a" * 32)):
        with pytest.raises(ExchangeError, match="live switch"):
            call()
    assert ("POST", "/api/v3/order") not in {(m, e) for m, e, _ in c.calls}
    assert ("DELETE", "/api/v3/orderList") not in {(m, e) for m, e, _ in c.calls}
    with pytest.raises(ExchangeError, match="authorize_live"):
        BinanceTrader(environment="live", client=c, authorization="yes")


def test_with_the_switch_a_live_order_is_sent():
    c = FakeClient().script(("POST", "/api/v3/order"), filled("x"))
    t = BinanceTrader(environment="live", client=c, authorization=authorize_live(LIVE_CFG, SWITCH))
    assert t.place(buy(), RULES).status == "FILLED"


def test_the_testnet_needs_no_switch_and_other_environments_stay_refused():
    BinanceTrader(environment="testnet", client=FakeClient())
    with pytest.raises(ExchangeError):
        BinanceTrader(environment="paper", client=FakeClient())


def test_the_confirmation_phrase_and_a_live_switch_are_written_nowhere_but_the_config_code():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for f in files:
        p = ROOT / f
        if not p.is_file() or p.suffix in (".pyc", ".png"):
            continue
        text = p.read_text(errors="ignore")
        if f != "src/bot/core/config.py":
            assert LIVE_CONFIRM_PHRASE not in text, f
        if p.suffix in (".yaml", ".yml"):
            assert "live_trading: true" not in text.lower(), f


# ---------------------------------------------------------------- the trading key
def test_a_trading_key_may_read_and_trade_spot_and_nothing_else():
    assert trading_key_problems(SAFE_KEY) == []
    for name in ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer", "enableMargin",
                 "enableFutures", "enableVanillaOptions", "enablePortfolioMarginTrading", "enableFixApiTrade"):
        assert f"{name} is ON; a trading key must have it off" in trading_key_problems({**SAFE_KEY, name: True}), name
    assert any("trusted IPs" in p for p in trading_key_problems({**SAFE_KEY, "ipRestrict": False}))
    assert any("cannot trade" in p for p in trading_key_problems({**SAFE_KEY, "enableSpotAndMarginTrading": False}))
    assert any("does not know" in p for p in trading_key_problems({**SAFE_KEY, "enableNewThing": True}))


# ---------------------------------------------------------------- the capped account
def live_world(tmp_path, usdt="300", capital="200"):
    ex = FakeBinance(usdt=usdt)
    ex.environment = "live"
    ex.authorization = authorize_live(LIVE_CFG, SWITCH)
    book = Ledger.load(tmp_path, "live")
    book.set_baseline({"BTC": Dec("1"), "ETH": Dec("1")}, "Sultan", "my own coins, not the bot's", T0)
    book.save()
    acct = TradingAccount.begin(tmp_path, LIVE_CFG, book, dict(ex.bal), ex.bal["USDT"], Dec(capital), "Sultan", T0,
                                cap_equity=True, max_capital=LIVE_CAPITAL_CEILING_USDT)
    return ex, book, acct


def test_the_live_capital_has_a_ceiling_in_code(tmp_path):
    book = Ledger.load(tmp_path, "live")
    book.baseline = {"BTC": Dec(1)}
    with pytest.raises(AccountError, match="ceiling"):
        TradingAccount.begin(tmp_path, LIVE_CFG, book, {}, Dec("100000"), LIVE_CAPITAL_CEILING_USDT + 1, "Sultan", T0,
                             cap_equity=True, max_capital=LIVE_CAPITAL_CEILING_USDT)
    assert LIVE_CAPITAL_CEILING_USDT == Dec("1000")


def test_trades_are_sized_on_the_capital_at_most_never_on_profits_or_deposits(tmp_path):
    ex, book, acct = live_world(tmp_path)
    bids = {"BTCUSDT": Dec("80000"), "ETHUSDT": Dec("2500")}
    richer = {**ex.bal, "USDT": ex.bal["USDT"] + Dec("500")}             # a profit, or USDT someone deposited
    st = acct.state(book, richer, richer["USDT"], bids, {}, T0 + 60)
    assert st.equity == Dec("200") and acct.last_equity == Dec("700")
    poorer = {**ex.bal, "USDT": ex.bal["USDT"] - Dec("5")}
    assert acct.state(book, poorer, poorer["USDT"], bids, {}, T0 + 120).equity == Dec("195")
    assert TradingAccount.load(tmp_path, LIVE_CFG, T0).cap_equity is True


def test_the_runner_trades_live_only_with_an_authorized_trader_and_a_live_ledger(tmp_path):
    ex, book, acct = live_world(tmp_path)
    data = MarketDataService(FakeMarket(), LIVE_CFG)
    TestnetRunner(tmp_path, LIVE_CFG, ex, data, data, [Idea()], {}, book, acct)
    ex.authorization = None
    with pytest.raises(RunnerError, match="live switch"):
        TestnetRunner(tmp_path, LIVE_CFG, ex, data, data, [Idea()], {}, book, acct)
    ex.authorization = authorize_live(LIVE_CFG, SWITCH)
    with pytest.raises(RunnerError, match="ledger is for testnet"):
        TestnetRunner(tmp_path, LIVE_CFG, ex, data, data, [Idea()], {}, Ledger.load(tmp_path / "t", "testnet"), acct)


def test_the_live_runner_names_the_live_resume_command(tmp_path):
    ex, book, acct = live_world(tmp_path)
    data = MarketDataService(FakeMarket(), LIVE_CFG)
    TestnetRunner(tmp_path, LIVE_CFG, ex, data, data, [Idea()], {}, book, acct).run(max_ticks=0)
    started = json.loads((tmp_path / "journal.jsonl").read_text().splitlines()[0])
    assert started["event"] == "started" and started["note"].endswith("bot live resume")


def test_alerts_watch_the_live_runner():
    assert alerts.SOURCES["live"] == "live"


# ---------------------------------------------------------------- bot live check / start / run
class LiveReader(FakeMarket):
    def __init__(self, perms):
        super().__init__()
        self.perms = perms

    def key_permissions(self):
        return argparse.Namespace(raw=self.perms)


def check(monkeypatch, perms=SAFE_KEY, switch=False):
    c = FakeClient().script(("POST", "/api/v3/order/test"), {}).script(
        ("GET", "/api/v3/account"), {"canTrade": True, "canWithdraw": True, "accountType": "SPOT",
                                     "permissions": ["SPOT", "MARGIN"],
                                     "balances": [{"asset": "USDT", "free": "250", "locked": "0"}]})
    monkeypatch.setattr(cli, "load_config", lambda path: LIVE_CFG if switch else AppConfig(environment="live"))
    monkeypatch.setattr(cli, "_live_environ", lambda: dict(SWITCH) if switch else {})
    out = io.StringIO()
    code = cli._live_check(argparse.Namespace(config="x"), make_trader=lambda **k: BinanceTrader(**k, client=c),
                           make_reader=lambda **k: LiveReader(perms), load_creds=lambda: object(), out=out)
    return code, out.getvalue(), c


def test_live_check_tests_the_key_and_trades_nothing(monkeypatch):
    code, text, c = check(monkeypatch)
    assert code == 0 and "SAFE: reading and Spot trading only" in text and "validated only, not placed" in text
    assert "Live switch .... OFF" in text and "250.00 USDT free" in text
    sent = {(m, e) for m, e, _ in c.calls}
    assert ("POST", "/api/v3/order/test") in sent and ("POST", "/api/v3/order") not in sent
    code, text, _ = check(monkeypatch, switch=True)
    assert "Live switch .... ON" in text and LIVE_CONFIRM_PHRASE not in text


def test_live_check_stops_on_a_key_that_can_withdraw(monkeypatch):
    code, text, c = check(monkeypatch, perms={**SAFE_KEY, "enableWithdrawals": True})
    assert code == 3 and "STOP" in text and not c.calls


def start_args(tmp_path, capital="200"):
    return argparse.Namespace(config="x", state_dir=str(tmp_path), operator="Sultan", capital=capital, env="live")


def run_start(tmp_path, monkeypatch, switch=True, capital="200", perms=SAFE_KEY):
    ex = FakeBinance(usdt="300")
    ex.environment = "live"
    book = Ledger.load(tmp_path, "live")
    book.set_baseline({"BTC": Dec("1"), "ETH": Dec("1")}, "Sultan", "my own coins, not the bot's", T0)
    book.save()
    monkeypatch.setattr(cli, "load_config", lambda path: LIVE_CFG if switch else AppConfig(environment="live"))
    monkeypatch.setattr(cli, "_live_environ", lambda: dict(SWITCH) if switch else {})
    out = io.StringIO()

    def make_trader(**k):
        ex.authorization = k.get("authorization")
        return ex
    code = cli._testnet_start(start_args(tmp_path, capital), make_trader=make_trader,
                              make_reader=lambda **k: LiveReader(perms), load_creds=lambda: object(), out=out)
    return code, out.getvalue()


def test_live_start_needs_the_switch_a_safe_key_and_a_capital_within_the_ceiling(tmp_path, monkeypatch, capsys):
    code, _ = run_start(tmp_path, monkeypatch, switch=False)
    assert code == 2 and "disabled in the config" in capsys.readouterr().err
    code, _ = run_start(tmp_path, monkeypatch, perms={**SAFE_KEY, "enableWithdrawals": True})
    assert code == 2 and "more than trade Spot" in capsys.readouterr().err
    code, _ = run_start(tmp_path, monkeypatch, capital="1001")
    assert code == 2 and "at most the free USDT" in capsys.readouterr().err   # 300 free: refused before the ceiling
    assert not (tmp_path / ACCOUNT_FILE).exists()
    code, text = run_start(tmp_path, monkeypatch, capital="200")
    assert code == 0 and "200.00 USDT of REAL money" in text and "bot live resume" in text
    assert TradingAccount.load(tmp_path, LIVE_CFG, T0).cap_equity is True


def test_live_run_needs_the_live_config_and_the_switch(tmp_path, monkeypatch, capsys):
    args = argparse.Namespace(config="x", state_dir=str(tmp_path), forward_dir=str(tmp_path), interval=0, ticks=1,
                              env="live")
    monkeypatch.setattr(cli, "load_config", lambda path: AppConfig())
    assert cli._testnet_run(args) == 2 and "live config" in capsys.readouterr().err
    from bot import forward
    monkeypatch.setattr(cli, "load_config", lambda path: LIVE_CFG)
    monkeypatch.setattr(cli, "_live_environ", lambda: {})
    monkeypatch.setattr(forward, "load", lambda d: argparse.Namespace(symbols=["BTCUSDT"], corr_groups={}))
    monkeypatch.setattr(forward, "check_unchanged", lambda ft, cfg: None)
    assert cli._testnet_run(args, load_creds=lambda: object()) == 2
    assert "confirmation phrase" in capsys.readouterr().err


def test_live_run_keeps_the_forward_tests_risk_settings(tmp_path, monkeypatch, capsys):
    """The live config may differ from the forward test's only in the live switch."""
    from bot import forward
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda path: LIVE_CFG)
    monkeypatch.setattr(forward, "load", lambda d: "ft")

    def check_unchanged(ft, cfg):
        seen["cfg"] = cfg
        raise forward.ForwardError("stop here")
    monkeypatch.setattr(forward, "check_unchanged", check_unchanged)
    args = argparse.Namespace(config="x", state_dir=str(tmp_path), forward_dir=str(tmp_path), interval=0, ticks=1,
                              env="live")
    assert cli._testnet_run(args) == 2
    assert forward.config_sha256(seen["cfg"]) == forward.config_sha256(AppConfig())
    assert config_mod.load_config(ROOT / "config" / "default.yaml").live_trading is False


def test_the_live_commands_parse_and_default_to_the_live_folder(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_testnet_status", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["live", "status"])
    assert seen["args"].env == "live" and seen["args"].state_dir == "~/live"
    seen.clear()
    monkeypatch.setattr(cli, "_reconcile_cmd", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["live", "baseline", "--operator", "Sultan", "--reason", "my own coins, not the bot's"])
    a = seen["args"]
    assert a.set_baseline and a.env == "live" and a.journal_dir == "~/live"
