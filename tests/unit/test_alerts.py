"""Module 5e: Telegram alerts. Journals in, short messages out; the token never shows; short outages
and repeats are held back; silence and a daily check are reported; the alert code cannot trade."""
import argparse
import ast
import io
import json
import os
from pathlib import Path

import pytest

from bot import alerts, cli
from bot.alerts import GREEN, RED, YELLOW, AlertService, Telegram, TelegramError, Watcher, describe, messages
from bot.exchange.credentials import CredentialsError, load_telegram, set_env_values

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
T0 = 1_792_627_200.0 + 3600             # 2026-10-22 01:00 UTC, before the daily check


def write(home, source, *events):
    d = home / alerts.SOURCES[source]
    d.mkdir(parents=True, exist_ok=True)
    with open(d / alerts.JOURNAL, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def home(tmp_path):
    for s in alerts.SOURCES:
        write(tmp_path, s, {"at": 0, "event": "started"})              # history before the service starts
    return tmp_path


def watcher(home, now=T0):
    return Watcher.open(home, home / "alerts", now)


# ---------------------------------------------------------------- what is worth a message
@pytest.mark.parametrize("event,level,words", [
    ({"event": "entry_protected", "symbol": "BTCUSDT", "qty": "0.0012", "price": "81000", "strategy": "H2b",
      "planned_risk": "2.5"}, GREEN, ["bought 0.0012 BTCUSDT at 81,000.00", "stop-loss and take-profit"]),
    ({"event": "trade_closed", "symbol": "BTCUSDT", "why": "time stop", "entry": "80000", "sold_at": "82000"},
     GREEN, ["closed BTCUSDT (time stop)", "sold at 82,000.00"]),
    ({"event": "trade_closed", "symbol": "ETHUSDT", "exit_reason": "STOP_LOSS", "entry_price": "2500",
      "exit_price": "2400", "pnl": "-4.1"}, GREEN, ["P&L -4.10 USDT after fees"]),
    ({"event": "unprotected", "detail": "BTCUSDT trade 1234: no confirmed protection"}, RED, ["ACT NOW"]),
    ({"event": "entry_unprotected", "symbol": "BTCUSDT", "reason": "emergency sale failed"}, RED, ["ACT NOW"]),
    ({"event": "reconciliation_mismatch", "detail": "BTC: Binance holds 1.5"}, RED, ["does not match"]),
    ({"event": "exit_failed", "symbol": "BTCUSDT", "why": "time stop", "reason": "MIN_NOTIONAL"}, RED, ["could not close"]),
    ({"event": "entry_unknown", "symbol": "BTCUSDT", "reason": "timeout"}, RED, ["outcome is unknown"]),
    ({"event": "breaker", "breach": "REJ_DAILY_LOSS_LIMIT"}, RED, ["breaker tripped"]),
    ({"event": "mode_changed", "to_mode": "RUNNING", "actor": "Sultan", "reason": "checked"}, GREEN, ["resumed by Sultan"]),
    ({"event": "mode_changed", "to_mode": "ENTRIES_BLOCKED", "actor": "system", "reason": "restarted"}, YELLOW,
     ["ENTRIES_BLOCKED"]),
    ({"event": "entry_blocked", "symbol": "BTCUSDT", "strategy": "H2b", "mode": "ENTRIES_BLOCKED"}, YELLOW,
     ["H2b wanted to buy BTCUSDT"]),
])
def test_events_that_matter_become_alerts(event, level, words):
    a = describe("testnet", event)
    assert a.level == level and all(w in a.line() for w in words), a.line()


@pytest.mark.parametrize("ev", ["status", "entry_rejected", "entry_not_filled", "ledger_update", "entries_unpaused"])
def test_routine_events_send_nothing(ev):
    assert describe("testnet", {"event": ev, "status": "OPEN"}) is None


def test_an_alert_is_always_one_line():
    a = describe("testnet", {"event": "exit_failed", "symbol": "BTCUSDT", "reason": "line one\nline two"})
    assert "\n" not in a.line()


# ---------------------------------------------------------------- following the journals
def test_history_from_before_the_start_is_never_replayed(home):
    w = watcher(home)
    assert w.poll(T0) == []
    write(home, "testnet", {"event": "breaker", "breach": "REJ_DRAWDOWN_BREAKER"})
    (a,) = w.poll(T0 + 30)
    assert a.level == RED and a.source == "testnet"


def test_a_restart_carries_on_where_it_stopped(home):
    w = watcher(home)
    write(home, "paper", {"event": "breaker", "breach": "REJ_DAILY_LOSS_LIMIT"})
    assert len(w.poll(T0 + 30)) == 1
    w.save()
    write(home, "paper", {"event": "breaker", "breach": "REJ_WEEKLY_LOSS_LIMIT"})
    again = watcher(home, T0 + 60)
    (a,) = again.poll(T0 + 60)
    assert "WEEKLY" in a.text                                      # not the daily one again


def test_a_half_written_line_waits_for_its_end(home):
    w = watcher(home)
    p = home / "testnet" / alerts.JOURNAL
    with open(p, "a") as f:
        f.write('{"event": "breaker", "breach": "X"')
    assert w.poll(T0 + 30) == []
    with open(p, "a") as f:
        f.write("}\n")
    assert len(w.poll(T0 + 60)) == 1


def test_an_unreadable_state_starts_fresh_and_says_so(home):
    (home / "alerts").mkdir()
    (home / "alerts" / alerts.STATE_FILE).write_text("{oops")
    w = watcher(home)
    (a,) = w.poll(T0)
    assert a.level == YELLOW and "unreadable" in a.text


# ---------------------------------------------------------------- holding back noise
def test_a_short_pause_is_swallowed_and_a_long_one_reported(home):
    w = watcher(home)
    write(home, "testnet", {"event": "entries_paused", "reason": "testnet down"})
    assert w.poll(T0 + 30) == []
    write(home, "testnet", {"event": "entries_unpaused"})
    assert w.poll(T0 + 120) == []                                   # back within the grace period
    write(home, "testnet", {"event": "entries_paused", "reason": "testnet down"})
    assert w.poll(T0 + 200) == []
    (a,) = w.poll(T0 + 200 + alerts.PAUSE_GRACE_S)
    assert a.level == YELLOW and "for over 10 minutes" in a.text
    write(home, "testnet", {"event": "entries_unpaused"})
    (b,) = w.poll(T0 + 200 + alerts.PAUSE_GRACE_S + 60)
    assert b.level == GREEN and "over" in b.text


def test_repeats_are_held_back_but_not_forever(home):
    w = watcher(home)
    e = {"event": "exit_failed", "symbol": "BTCUSDT", "why": "time stop", "reason": "MIN_NOTIONAL"}
    write(home, "testnet", e)
    assert len(w.poll(T0 + 30)) == 1
    write(home, "testnet", e)
    assert w.poll(T0 + 90) == []
    write(home, "testnet", e)
    assert len(w.poll(T0 + 30 + alerts.REPEAT_S)) == 1


def test_good_news_is_never_held_back(home):
    w = watcher(home)
    for i in range(3):
        write(home, "paper", {"event": "trade_closed", "symbol": "BTCUSDT", "exit_reason": "TP", "entry_price": "1",
                              "exit_price": "2", "pnl": str(i)})
    assert len(w.poll(T0 + 30)) == 3


# ---------------------------------------------------------------- silence and the daily check
def test_a_silent_runner_is_reported_once_then_its_return(home):
    w = watcher(home)
    write(home, "testnet", {"event": "status", "equity": "1000", "mode": "RUNNING"})
    w.poll(T0 + 30)
    later = T0 + 30 + alerts.SILENT_S
    silent = [a for a in w.poll(later) if a.source == "testnet"]
    assert silent and silent[0].level == RED and "no sign of life" in silent[0].text
    assert not [a for a in w.poll(later + 60) if a.source == "testnet"]
    write(home, "testnet", {"event": "status", "equity": "1000", "mode": "RUNNING"})
    back = [a for a in w.poll(later + 120) if a.source == "testnet"]
    assert back[0].level == GREEN and "running again" in back[0].text


def test_the_daily_check_says_each_runners_mode_and_equity(home):
    w = watcher(home)
    write(home, "testnet", {"event": "status", "equity": "1000", "mode": "RUNNING"})
    write(home, "paper", {"event": "status", "equity": "9994.79", "mode": "RUNNING"})
    assert w.poll(T0 + 30) == []
    morning = T0 + 3 * 3600 + 60                                    # 04:01 UTC, 08:01 Muscat
    for s in ("testnet", "paper", "forward"):
        write(home, s, {"event": "noop"})
    (a,) = w.poll(morning)
    assert a.source == "daily check" and "testnet RUNNING, equity 1,000.00" in a.text
    assert "paper RUNNING, equity 9,994.79" in a.text and "forward: no status yet" in a.text
    assert w.poll(morning + 60) == []                               # once a day


# ---------------------------------------------------------------- Telegram
class Fake:
    def __init__(self, fail=False):
        self.sent, self.fail, self.urls = [], fail, []

    def __call__(self, url, params):
        self.urls.append(url)
        if self.fail:
            raise OSError(f"cannot reach {url}")
        if url.endswith("/sendMessage"):
            self.sent.append(params["text"])
        return {"ok": True, "result": {}}


def test_the_token_never_appears_in_an_error_or_repr():
    t = Telegram(TOKEN, "42", transport=Fake(fail=True))
    with pytest.raises(TelegramError) as e:
        t.send("hi")
    assert TOKEN not in str(e.value) and "<token>" in str(e.value)
    assert TOKEN not in repr(t)


def test_telegram_refusing_is_an_error():
    t = Telegram(TOKEN, "42", transport=lambda url, p: {"ok": False, "description": "chat not found"})
    with pytest.raises(TelegramError, match="chat not found"):
        t.send("hi")


def test_lines_are_packed_into_few_messages():
    out = messages(["x" * 2000, "y" * 2000, "z"])
    assert len(out) == 2 and out[0] == "x" * 2000 and out[1] == "y" * 2000 + "\nz"


def test_unsent_alerts_wait_for_telegram_and_are_sent_later(home):
    fake = Fake(fail=True)
    svc = AlertService(watcher(home), Telegram(TOKEN, "42", transport=fake), clock=lambda: T0 + 30, log=lambda m: None)
    write(home, "testnet", {"event": "breaker", "breach": "X"})
    svc.tick()
    assert len(svc.queue) == 1 and not fake.sent
    fake.fail = False
    svc.tick()
    assert not svc.queue and "breaker tripped" in fake.sent[0]


def test_settings_load_and_never_show_the_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=abc\n")
    os.chmod(env, 0o600)
    set_env_values(env, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
    assert "BINANCE_API_KEY=abc" in env.read_text() and oct(env.stat().st_mode)[-3:] == "600"
    t = load_telegram(environ={"BOT_ENV_FILE": str(env)})
    assert t.chat_id == "42" and TOKEN not in repr(t)
    set_env_values(env, {"TELEGRAM_CHAT_ID": "43"})
    assert env.read_text().count("TELEGRAM_CHAT_ID") == 1
    with pytest.raises(CredentialsError, match="does not look like"):
        load_telegram(environ={"BOT_ENV_FILE": str(env), "TELEGRAM_BOT_TOKEN": "nonsense"})


def test_the_alert_code_cannot_trade():
    tree = ast.parse((SRC / "alerts.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("bot") for m in mods), mods
    text = (SRC / "alerts.py").read_text()
    assert "binance_sdk" not in text and "api.binance" not in text and "binance.vision" not in text


# ---------------------------------------------------------------- bot alerts setup
class SetupFake(Fake):
    def __init__(self, chats):
        super().__init__()
        self.chats = chats

    def __call__(self, url, params):
        if url.endswith("/getMe"):
            return {"ok": True, "result": {"username": "sultan_crypto_bot"}}
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": [{"message": {"chat": c}} for c in self.chats]}
        return super().__call__(url, params)


def run_setup(tmp_path, monkeypatch, answer="yes", token=TOKEN, chats=None):
    env = tmp_path / ".env"
    monkeypatch.setenv("BOT_ENV_FILE", str(env))
    fake = SetupFake(chats if chats is not None else [{"id": 42, "type": "private", "first_name": "Sultan"}])
    out = io.StringIO()
    code = cli._alerts_setup(argparse.Namespace(), read_secret=lambda p: token, ask=lambda p: answer,
                             telegram=lambda tok: Telegram(tok, transport=fake), sleep=lambda s: None, out=out)
    return code, out.getvalue(), env, fake


def test_setup_saves_the_chat_after_you_confirm_and_sends_a_test(tmp_path, monkeypatch):
    code, text, env, fake = run_setup(tmp_path, monkeypatch)
    assert code == 0 and "@sultan_crypto_bot" in text and TOKEN not in text
    assert "TELEGRAM_CHAT_ID=42" in env.read_text() and "set up" in fake.sent[0]


def test_setup_saves_nothing_if_it_is_not_you_or_nobody_wrote(tmp_path, monkeypatch, capsys):
    code, _, env, _ = run_setup(tmp_path, monkeypatch, answer="no")
    assert code == 2 and not env.exists()
    code, _, env, _ = run_setup(tmp_path, monkeypatch, chats=[{"id": -5, "type": "group"}])
    assert code == 2 and not env.exists() and "no message" in capsys.readouterr().err
    code, _, env, _ = run_setup(tmp_path, monkeypatch, token="not-a-token")
    assert code == 2 and not env.exists()


def test_a_message_that_went_is_not_sent_again_when_a_later_one_fails(home):
    calls = []

    def flaky(url, params):
        calls.append(params["text"])
        if len(calls) == 2:
            raise OSError("network")
        return {"ok": True, "result": {}}
    svc = AlertService(watcher(home), Telegram(TOKEN, "42", transport=flaky), clock=lambda: T0 + 30, log=lambda m: None)
    svc.queue = ["a" * 3000, "b" * 3000]
    svc.tick()
    assert svc.queue == ["b" * 3000]
    svc.tick()
    assert not svc.queue and calls[-1] == "b" * 3000


def test_the_settings_file_stays_private_even_after_a_crash_left_a_temp_file(tmp_path):
    env = tmp_path / ".env"
    (tmp_path / ".env.tmp").write_text("left over")
    os.chmod(tmp_path / ".env.tmp", 0o644)
    set_env_values(env, {"TELEGRAM_CHAT_ID": "42"})
    assert oct(env.stat().st_mode)[-3:] == "600"
