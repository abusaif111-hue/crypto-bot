"""`bot entries`: why H2b is not entering, per account and coin (read-only diagnosis)."""
import argparse
import ast
import hashlib
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli, entry_diagnosis as ed, forward
from bot.analysis.resample import DAY_MS
from bot.backtest.periods import HOLDOUT_END_MS
from bot.core.config import AppConfig
from bot.core.ids import client_order_id, new_intent_id
from bot.exchange.trading import ProtectedEntryRequest
from bot.risk.models import RejectCode
from bot.strategies.h2b_blend import BlendedMomentum
from tests.backtest_data import rules as fixture_rules
from tests.unit.test_h2b import CALM_RISE, path
from tests.unit.test_strategies import series

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "bot" / "entry_diagnosis.py"
H2B = BlendedMomentum(["BTCUSDT"]).with_params(threshold=Dec("0.60"), atr_mult=Dec("3"))
CFG = AppConfig(universe=["BTCUSDT", "ETHUSDT"])
RULES = fixture_rules()
DAY = HOLDOUT_END_MS + 19 * DAY_MS                       # 2026-10-20 00:00 UTC, a decision day
FALL = [(380, 0.0, 0.02), (60, -0.004, 0.01)]


def history(daily_closes, last_day_ms=DAY):
    """Six 4h candles a day; the last one closes just before `last_day_ms` (00:00 UTC)."""
    return series([c for c in daily_closes for _ in range(6)], start=last_day_ms - len(daily_closes) * DAY_MS)


def sig_at(closes, day=DAY, strategy=H2B):
    return ed.day_signal(strategy, history(closes, day), "BTCUSDT", day)


# ---------------------------------------------------------------- the signal is H2b's own
def test_the_verdict_is_h2bs_own_on_the_candles_closed_before_the_decision():
    for closes in (path(CALM_RISE), path(FALL), path(CALM_RISE)[:200]):
        cs = history(closes)
        s = ed.day_signal(H2B, cs, "BTCUSDT", DAY)
        assert (s.enter, s.why or "") == (H2B.signal(H2B._daily(cs))[0], H2B.signal(H2B._daily(cs))[1] or "")


def test_later_candles_never_change_a_days_verdict():
    cs = history(path(CALM_RISE))
    crash = series([1.0] * 60, start=DAY)                               # after the decision: a collapse
    assert ed.day_signal(H2B, cs + crash, "BTCUSDT", DAY) == ed.day_signal(H2B, cs, "BTCUSDT", DAY)


def test_a_signal_carries_h2bs_entry_and_stop():
    s = sig_at(path(CALM_RISE))
    assert s.enter and not s.why and s.stop < s.entry and s.stop_pct > 0
    cs = history(path(CALM_RISE))
    entry = cs[-1].close * Dec("1.001")
    assert s.entry == entry and s.stop == H2B.exit_plan(cs, entry)[0]


def test_no_signal_keeps_h2bs_reason():
    s = sig_at(path(FALL))
    assert not s.enter and "lookbacks strong" in s.why


# ---------------------------------------------------------------- never the spent year
def test_days_before_the_end_of_the_spent_holdout_year_are_never_measured():
    sep = (HOLDOUT_END_MS - 30 * DAY_MS) / 1000
    days = ed.decision_days(sep, DAY / 1000)
    assert days[0] == HOLDOUT_END_MS and days[-1] == DAY and len(days) == 20
    assert ed.decision_days(sep, DAY / 1000, since_ms=HOLDOUT_END_MS - 90 * DAY_MS)[0] == HOLDOUT_END_MS
    assert ed.decision_days(None, DAY / 1000) == []
    mid = (DAY - 5 * 3600_000) / 1000                                   # started at 19:00 the day before
    assert ed.decision_days(mid, DAY / 1000 + 60) == [DAY]


# ---------------------------------------------------------------- the minimum order, the engine's own answer
def probe(stop_pct, entry=Dec("80000")):
    return ed.DaySignal("BTCUSDT", DAY, True, "", entry, entry * (1 - Dec(stop_pct) / 100), Dec(stop_pct))


def test_a_wide_stop_cannot_meet_the_minimum_on_100_usdt_but_fits_a_larger_account():
    ok, code = ed.engine_check(probe("9"), Dec("100"), RULES["BTCUSDT"], CFG)
    assert not ok and code == RejectCode.EXCHANGE_MIN.value
    assert ed.engine_check(probe("9"), Dec("10000"), RULES["BTCUSDT"], CFG) == (True, None)
    assert ed.engine_check(probe("3"), Dec("100"), RULES["BTCUSDT"], CFG)[0]


def test_the_capital_needed_is_the_smallest_the_engine_accepts():
    for pct_ in ("4.5", "9", "12"):
        need = ed.capital_needed(probe(pct_), RULES["BTCUSDT"], CFG)
        assert ed.engine_check(probe(pct_), need, RULES["BTCUSDT"], CFG)[0]
        assert not ed.engine_check(probe(pct_), need - 1, RULES["BTCUSDT"], CFG)[0]
    # 9% stop: the engine alone accepts about 220 USDT; the protective orders (fees, 2% margin) need more
    assert 240 < ed.capital_needed(probe("9"), RULES["BTCUSDT"], CFG) <= 260


def test_the_risk_settings_are_never_changed_to_fit():
    before = CFG.model_dump()
    ed.capital_needed(probe("9"), RULES["BTCUSDT"], CFG)
    assert CFG.model_dump() == before and CFG.risk.risk_per_trade_pct == Dec("0.25")


@pytest.mark.parametrize("qty,stop", [("0.00007", "75000"), ("0.00008", "70000"), ("0.0001", "52000"),
                                      ("0.0001", "50000"), ("0.00006", "86000"), ("0.01", "70000")])
def test_the_protection_check_is_the_traders_own(qty, stop):
    """protected_ok must agree with ProtectedEntryRequest.problems ('too small to protect')."""
    iid = new_intent_id()
    r = RULES["BTCUSDT"]
    req = ProtectedEntryRequest("BTCUSDT", Dec(qty), Dec("80000"), Dec(stop), Dec("90000"), "STOP_LOSS",
                                client_order_id(iid, "list"), client_order_id(iid, "entry"),
                                client_order_id(iid, "protect", 0), client_order_id(iid, "protect", 1))
    too_small = any(ed.TOO_SMALL in p for p in req.problems(r, ed.FEE))
    assert ed.protected_ok(Dec(qty), Dec(stop), r) is not too_small


# ---------------------------------------------------------------- one category per coin-day
SIG = probe("9")
NOSIG = ed.DaySignal("BTCUSDT", DAY, False, "1 of 3 lookbacks strong")


def at(minutes):
    return DAY / 1000 + minutes * 60


def ev(event, minutes=1, **kw):
    return {"at": at(minutes), "event": event, "symbol": "BTCUSDT", "strategy": "H2b", **kw}


@pytest.mark.parametrize("events,category", [
    ([ev("entry_rejected", code="REJ_BELOW_EXCHANGE_MIN", reason="size rounds to zero")], ed.MIN_ORDER),
    ([ev("entry_refused", reason="not sent: too small to protect: the stop order would be worth 4.1 USDT")],
     ed.MIN_ORDER),
    ([ev("entry_not_filled", reason="fill-or-kill: the price moved away; nothing was bought")], ed.NOT_FILLED),
    ([ev("entry_blocked", mode="ENTRIES_BLOCKED", breaches=[], reason=None)], ed.BLOCKED),
    ([ev("entry_blocked", mode="ENTRIES_BLOCKED", reason="could not read Binance")], ed.BLOCKED),
    ([ev("entry_rejected", code="REJ_MODE", reason="new entries are not allowed in mode ENTRIES_BLOCKED")],
     ed.BLOCKED),
    ([ev("entry_protected", reason="filled; stop-loss and take-profit confirmed open on Binance")], ed.ENTERED),
    ([ev("entry_filled")], ed.ENTERED),
    ([ev("entry_rejected", code="REJ_CORRELATION_GROUP", reason="group full")], ed.OTHER),
    ([ev("entry_skipped", reason="no bid")], ed.OTHER),
    ([ev("entry_unknown", reason="no answer")], ed.OTHER),
    ([ev("entry_emergency_exit", reason="protection not confirmed")], ed.OTHER),
])
def test_what_the_runner_did_decides_the_category(events, category):
    assert ed.classify(SIG, [{"at": at(0), "event": "status"}] + events)[0] == category


def test_no_signal_is_category_one_with_h2bs_reason():
    assert ed.classify(NOSIG, [{"at": at(1), "event": "status"}]) == (ed.NO_SIGNAL, "1 of 3 lookbacks strong")


def test_a_signal_with_nothing_in_the_decision_window_is_a_missed_decision():
    cat, why = ed.classify(SIG, [{"at": at(300), "event": "started"}])          # back at 05:00, too late
    assert cat == ed.BLOCKED and "missed" in why
    cat, why = ed.classify(SIG, [{"at": at(2), "event": "strategy_error", "reason": "KeyError"}])
    assert cat == ed.BLOCKED and "strategy_error" in why


def test_a_signal_the_journal_does_not_explain_is_flagged_not_hidden():
    cat, why = ed.classify(SIG, [{"at": at(1), "event": "status"}])
    assert cat == ed.OTHER and "no idea" in why


def test_other_coins_strategies_and_manual_trades_are_not_counted():
    events = [ev("entry_protected", symbol="ETHUSDT"), ev("entry_filled", strategy="manual")]
    assert ed.classify(SIG, [{"at": at(0), "event": "status"}] + events)[0] == ed.OTHER


def test_a_signal_without_a_valid_stop_is_other():
    s = ed.DaySignal("BTCUSDT", DAY, True, "no valid stop (daily ATR)", Dec("100"))
    assert ed.classify(s, [{"at": at(0), "event": "status"}]) == (ed.OTHER, "no valid stop (daily ATR)")


# ---------------------------------------------------------------- whole accounts, from their files
def make_home(tmp_path, journal, capital="100", mode="RUNNING", breaches=(), started=None):
    d = tmp_path / "live"
    d.mkdir()
    started = (DAY - 5 * 3600_000) / 1000 if started is None else started
    (d / "account.json").write_text(json.dumps({
        "capital": capital, "started_at": started,
        "modes": {"mode": mode, "active_breaches": list(breaches),
                  "history": [{"at": "2026-10-19T19:00:00+00:00", "from_mode": "ENTRIES_BLOCKED",
                               "to_mode": mode, "actor": "Sultan", "reason": "resumed after the update"}]}}))
    (d / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in journal) + "not json\n")
    return tmp_path


def snapshot(folder):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(folder).rglob("*") if p.is_file()}


def test_an_account_is_diagnosed_per_coin_and_its_files_are_left_alone(tmp_path):
    home = make_home(tmp_path, [{"at": at(0), "event": "status", "mode": "RUNNING"},
                                ev("entry_rejected", code="REJ_BELOW_EXCHANGE_MIN", reason="size rounds to zero")],
                     capital="50")                                # the calm fixture's stop (3.3%) needs about 86
    before = snapshot(home)
    s = BlendedMomentum(["BTCUSDT", "ETHUSDT"]).with_params(threshold=Dec("0.60"), atr_mult=Dec("3"))
    candles = {"BTCUSDT": history(path(CALM_RISE)), "ETHUSDT": history(path(FALL))}
    dx = ed.diagnose(home, s, CFG, candles, RULES, DAY / 1000 + 3600, names=["live"])
    assert snapshot(home) == before
    got = {(d.symbol, d.category) for d in dx.decisions}
    assert got == {("BTCUSDT", ed.MIN_ORDER), ("ETHUSDT", ed.NO_SIGNAL)}
    btc = next(d for d in dx.decisions if d.symbol == "BTCUSDT")
    assert btc.signal.min_order_ok is False and btc.signal.capital_needed > 50
    c = ed.counts(dx.decisions)
    assert c["all"][ed.MIN_ORDER] == 1 and c["all"][ed.NO_SIGNAL] == 1 and sum(c["BTCUSDT"].values()) == 1
    text = ed.render(dx, CFG)
    assert "2 signal, below Binance's minimum order" in text and "100.0%" in text and "50.0%" in text
    assert "BELOW THE MINIMUM" in text and "needs at least" in text and "mode RUNNING" in text
    assert "min order 5 USDT" in text and "step 0.00001" in text and "STOP_LOSS yes" in text
    assert "resumed after the update" in text
    assert "on 0 coin-days where the recomputed signal said no (as it should be)" in text


def test_a_journal_idea_the_recomputed_signal_does_not_have_is_counted(tmp_path):
    home = make_home(tmp_path, [{"at": at(0), "event": "status"}, ev("entry_blocked", mode="ENTRIES_BLOCKED")])
    dx = ed.diagnose(home, H2B, CFG, {"BTCUSDT": history(path(FALL))}, RULES, DAY / 1000 + 3600, names=["live"])
    assert "on 1 coin-days where the recomputed signal said no (these should be 0" in ed.render(dx, CFG)


def test_a_blocked_account_says_so_first(tmp_path):
    home = make_home(tmp_path, [{"at": at(0), "event": "status", "mode": "ENTRIES_BLOCKED"}],
                     mode="ENTRIES_BLOCKED", breaches=["RECONCILIATION_MISMATCH"])
    dx = ed.diagnose(home, H2B, CFG, {"BTCUSDT": history(path(FALL))}, RULES, DAY / 1000 + 3600, names=["live"])
    text = ed.render(dx, CFG)
    assert "ENTRIES ARE BLOCKED NOW" in text and "RECONCILIATION_MISMATCH" in text


def test_an_account_started_in_the_spent_year_is_measured_from_1_october_only(tmp_path):
    home = make_home(tmp_path, [], started=(HOLDOUT_END_MS - 40 * DAY_MS) / 1000)
    dx = ed.diagnose(home, H2B, CFG, {"BTCUSDT": history(path(FALL))}, RULES, DAY / 1000 + 3600, names=["live"])
    assert dx.decisions and min(d.day_ms for d in dx.decisions) == HOLDOUT_END_MS


def test_missing_folders_and_bad_files_are_reported_not_fatal(tmp_path):
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "state.json").write_text("{broken")
    dx = ed.diagnose(tmp_path, H2B, CFG, {}, RULES, DAY / 1000)
    text = ed.render(dx, CFG)
    assert "LIVE" in text and "no folder" in text and "state.json is unreadable" in text


# ---------------------------------------------------------------- read-only by design
def test_it_cannot_trade_or_write():
    text = SRC.read_text()
    tree = ast.parse(text)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith(("bot.execution", "bot.exchange.trading", "bot.exchange.binance", "bot.paper",
                                       "bot.forward", "bot.backtest.history", "bot.backtest.holdout"))
                   for m in mods), mods
    for bad in ("write_text", "os.replace", "unlink", "mkdir", '"w"', "'w'", '"a"', "'a'", ".env"):
        assert bad not in text.replace("never reads `.env`", ""), bad


def test_h2b_is_not_touched():
    """The forward test fingerprints H2b's code; this command must not be part of it."""
    assert "entry_diagnosis" not in "".join(forward._SOURCES)


# ---------------------------------------------------------------- the command
class Venue:
    environment = "live"

    def __init__(self, candles):
        self.candles = candles

    def rules(self, symbol):
        return RULES[symbol]

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        return self.candles[symbol][-limit:]


def test_the_command_reads_live_rules_and_prices_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from bot.market import data as market_data
    home = make_home(tmp_path, [{"at": at(0), "event": "status", "mode": "RUNNING"}])
    before = snapshot(home)
    ft = argparse.Namespace(symbols=["BTCUSDT"])
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda p: AppConfig())
    monkeypatch.setattr(forward, "load", lambda d: ft)
    monkeypatch.setattr(forward, "make_strategy", lambda f: H2B)

    def mds(exchange, cfg):
        seen["env"], seen["universe"] = exchange.environment, cfg.universe
        return Venue({"BTCUSDT": history(path(CALM_RISE))})
    monkeypatch.setattr(market_data, "MarketDataService", mds)
    args = argparse.Namespace(config="x", forward_dir="~/f", home=str(home), since=None, accounts=["live"])
    assert cli._entries(args, make_exchange=lambda environment: argparse.Namespace(environment=environment),
                        now=DAY / 1000 + 3600) == 0
    out = capsys.readouterr().out
    assert seen == {"env": "live", "universe": ["BTCUSDT"]}
    assert "Why H2b is not entering" in out and "LIVE" in out and "Binance live rules" in out
    assert snapshot(home) == before


def test_the_command_reports_a_binance_outage(tmp_path, monkeypatch, capsys):
    from bot.market import data as market_data
    home = make_home(tmp_path, [])
    monkeypatch.setattr(cli, "load_config", lambda p: AppConfig())
    monkeypatch.setattr(forward, "load", lambda d: argparse.Namespace(symbols=["BTCUSDT"]))
    monkeypatch.setattr(forward, "make_strategy", lambda f: H2B)

    def down(exchange, cfg):
        raise ConnectionError("blocked")
    monkeypatch.setattr(market_data, "MarketDataService", down)
    args = argparse.Namespace(config="x", forward_dir="~/f", home=str(home), since=None, accounts=None)
    assert cli._entries(args, make_exchange=lambda environment: None, now=DAY / 1000 + 3600) == 1
    assert "could not read Binance" in capsys.readouterr().out


def test_the_command_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_entries", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["entries", "--since", "2026-10-20", "--accounts", "live", "forward"])
    assert seen["args"].since == "2026-10-20" and seen["args"].accounts == ["live", "forward"]
