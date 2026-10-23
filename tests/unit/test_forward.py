"""The H2b forward test (docs/h2b-plan.md step C): fixed once at the start, refuses changed code or config,
its own account with no manual trades, and the end dates from the plan."""
import argparse
import io
import json
from dataclasses import replace
from decimal import Decimal as Dec

import pytest

from bot import cli, forward
from bot.backtest import history, universe
from bot.backtest.periods import HOLDOUT_START_MS
from bot.core.config import AppConfig, load_config
from bot.paper.runner import CLOCK_FILE, STATE_FILE, PaperRunner, write_request
from bot.risk.models import RejectCode
from bot.strategies.h2b_blend import BlendedMomentum
from tests.backtest_data import H4, rules, walk
from tests.fakes import Clock, FakeMarket

COINS = list(forward.PLAN_GROUPS)
GROUPS = {"XRPUSDT": "XRPUSDT", "BNBUSDT": "BNBUSDT"}          # as the screen saves them (BTC/ETH: majors)
DAY = forward.DAY_S
T0 = 1_792_627_200.0                                            # 2026-10-22 00:00 UTC


def research(n=50):
    start = HOLDOUT_START_MS - n * H4
    return {s: walk(start, n, "100", i + 1) for i, s in enumerate(COINS)}


class FakeChoose:
    def __init__(self):
        self.calls = []

    def __call__(self, strategy, candles, rules_, cfg, cost, w, groups):
        self.calls.append((strategy, candles, w, groups))
        return strategy.with_params(atr_mult=Dec("3.6")), Dec("1.5")


@pytest.fixture
def fake_choose(monkeypatch):
    f = FakeChoose()
    monkeypatch.setattr(forward, "choose", f)
    return f


def started(tmp_path, fake_choose, now=T0, **kw):
    cfg = AppConfig()
    wide = cfg.model_validate({**cfg.model_dump(), "universe": COINS})
    args = dict(operator="Sultan", confirm=forward.CONFIRM_PHRASE, groups=GROUPS, git_commit="abc")
    args.update(kw)
    return forward.start(tmp_path, research(), rules(COINS), wide, cfg, now=now, **args)


# ---------------------------------------------------------------- start: once, before any trade
def test_start_fixes_settings_coins_and_fingerprints(tmp_path, fake_choose):
    ft = started(tmp_path, fake_choose)
    saved = json.loads((tmp_path / forward.FORWARD_FILE).read_text())
    assert saved["params"] == {"threshold": "0.5", "atr_mult": "3.6"} and saved["symbols"] == COINS
    assert saved["corr_groups"] == forward.PLAN_GROUPS and saved["started_at"] == T0
    assert ft == forward.load(tmp_path)
    assert ft.strategy_sha256 == forward.strategy_sha256() and ft.config_sha256 == forward.config_sha256(AppConfig())
    (strategy, candles, w, groups), = fake_choose.calls
    assert isinstance(strategy, BlendedMomentum) and strategy.params() == BlendedMomentum(COINS).params()
    assert (w.train_start, w.train_end) == (forward._ms(2023, 10), HOLDOUT_START_MS)   # the last 24 research months
    assert all(c.close_time < HOLDOUT_START_MS for cs in candles.values() for c in cs)  # never the spent holdout


@pytest.mark.parametrize("bad", [dict(confirm="yes"), dict(operator=" "), dict(groups={})])
def test_start_refuses_mistakes_and_writes_nothing(tmp_path, fake_choose, bad):
    with pytest.raises(forward.ForwardError):
        started(tmp_path, fake_choose, **bad)               # groups={}: XRP and BNB would join BTC/ETH
    assert not (tmp_path / forward.FORWARD_FILE).exists() and fake_choose.calls == []


def test_a_forward_test_never_restarts_in_place(tmp_path, fake_choose):
    started(tmp_path, fake_choose)
    before = (tmp_path / forward.FORWARD_FILE).read_text()
    with pytest.raises(forward.ForwardError):
        started(tmp_path, fake_choose, now=T0 + DAY)
    assert (tmp_path / forward.FORWARD_FILE).read_text() == before
    other = tmp_path / "used"
    other.mkdir()
    (other / STATE_FILE).write_text("{}")                   # a folder that already holds an account
    with pytest.raises(forward.ForwardError):
        started(other, fake_choose)


def test_a_start_racing_another_never_overwrites_it(tmp_path, monkeypatch):
    def other_start_meanwhile(strategy, *a):
        (tmp_path / forward.FORWARD_FILE).write_text('{"the": "first start"}')
        return strategy, None
    monkeypatch.setattr(forward, "choose", other_start_meanwhile)
    with pytest.raises(forward.ForwardError, match="was kept"):
        started(tmp_path, None)
    assert json.loads((tmp_path / forward.FORWARD_FILE).read_text()) == {"the": "first start"}


def test_only_the_plans_coins(tmp_path):
    with pytest.raises(forward.ForwardError):
        forward.check_coins(["BTCUSDT", "ETHUSDT", "XRPUSDT"], GROUPS)
    with pytest.raises(forward.ForwardError):
        forward.check_coins(COINS + ["SOLUSDT"], {**GROUPS, "SOLUSDT": "SOLUSDT"})
    forward.check_coins(COINS, GROUPS)


# ---------------------------------------------------------------- nothing about H2b changes during the test
def test_changed_code_or_config_stops_the_test(tmp_path, fake_choose, monkeypatch):
    ft = started(tmp_path, fake_choose)
    forward.check_unchanged(ft, AppConfig())
    looser = AppConfig().model_validate({**AppConfig().model_dump(), "risk": {**AppConfig().risk.model_dump(),
                                                                            "risk_per_trade_pct": Dec("0.2")}})
    with pytest.raises(forward.ForwardError, match="config changed"):
        forward.check_unchanged(ft, looser)
    monkeypatch.setattr(forward, "strategy_sha256", lambda: "edited")
    with pytest.raises(forward.ForwardError, match="restarts the test from zero"):
        forward.check_unchanged(ft, AppConfig())


def test_strategy_fingerprint_covers_h2b_and_its_base(tmp_path, monkeypatch):
    real = forward.strategy_sha256()
    fake_root = tmp_path / "bot"
    (fake_root / "strategies").mkdir(parents=True)
    h2b_and_base = ("strategies/h2b_blend.py", "strategies/base.py")
    for rel in h2b_and_base:
        src = (forward.Path(forward.__file__).parent / rel).read_text()
        (fake_root / rel).write_text(src)
    monkeypatch.setattr(forward, "__file__", str(fake_root / "forward.py"))
    assert forward.strategy_sha256() == real
    for rel in h2b_and_base:
        (fake_root / rel).write_text((fake_root / rel).read_text() + "\n# edited\n")
        assert forward.strategy_sha256() != real
        (fake_root / rel).write_text((fake_root / rel).read_text().removesuffix("\n# edited\n"))


def test_the_saved_settings_are_the_ones_traded(tmp_path, fake_choose):
    s = forward.make_strategy(started(tmp_path, fake_choose))
    assert s.params() == {"threshold": Dec("0.5"), "atr_mult": Dec("3.6")} and s.symbols == COINS
    with pytest.raises(forward.ForwardError):
        forward.make_strategy(replace(forward.load(tmp_path), params={"lookback": "4"}))


# ---------------------------------------------------------------- the end dates (plan section 3)
def ft_at(t=T0):
    return forward.ForwardTest("H2b", {}, COINS, forward.PLAN_GROUPS, t, "Sultan", None, "c", "s", "abc")


def last_candle(now):
    return int(now * 1000) // (H4) * H4


def test_at_least_12_months_and_30_trades_at_most_24():
    ft = ft_at()
    p = forward.progress(ft, [], last_candle(T0 + 10 * DAY), 3, T0 + 10 * DAY)
    assert p.state == "running" and p.earliest_end == T0 + 365 * DAY and p.latest_end == T0 + 730 * DAY
    year = T0 + 366 * DAY
    assert forward.progress(ft, [], last_candle(year), 29, year).state == "running"        # 12 months, 29 trades
    assert forward.progress(ft, [], last_candle(year), 30, year).state == "ready to judge"
    assert forward.progress(ft, [], last_candle(T0 + 200 * DAY), 80, T0 + 200 * DAY).state == "running"
    end = T0 + 731 * DAY
    assert forward.progress(ft, [], last_candle(end), 12, end).state == "ended"            # judged with 12 trades


def test_outages_above_10_percent_move_both_end_dates():
    ft = ft_at()
    now = T0 + 100 * DAY
    small = [{"event": "decisions_missed", "candles": 6 * 9}]       # 9 days of 100: no change
    p = forward.progress(ft, small, last_candle(now), 0, now)
    assert p.missed_hours == 9 * 24 and p.earliest_end == T0 + 365 * DAY
    big = [{"event": "decisions_missed", "candles": 6 * 11}]        # 11 days of 100: dates move by 11 days
    p = forward.progress(ft, big, last_candle(now), 0, now)
    assert p.earliest_end == T0 + 376 * DAY and p.latest_end == T0 + 741 * DAY
    down_now = forward.progress(ft, [], last_candle(now - 11 * DAY), 0, now)   # an outage still going on counts
    assert down_now.missed_hours >= 11 * 24 - 8 and down_now.earliest_end > T0 + 365 * DAY


def test_the_drawdown_breaker_fails_h2b_at_once():
    ft = ft_at()
    j = [{"event": "breaker", "breach": RejectCode.DAILY_LOSS.value, "at": T0 + DAY},
         {"event": "breaker", "breach": RejectCode.DRAWDOWN.value, "at": T0 + 40 * DAY}]
    p = forward.progress(ft, j, last_candle(T0 + 50 * DAY), 5, T0 + 50 * DAY)
    assert p.state == "failed" and p.failed_at == T0 + 40 * DAY
    assert forward.progress(ft, j[:1], last_candle(T0 + 50 * DAY), 5, T0 + 50 * DAY).state == "running"


# ---------------------------------------------------------------- its own account
def test_no_manual_trades_and_the_groups_survive_a_restart(tmp_path):
    clock, market = Clock(), FakeMarket()
    kw = dict(manual_trades=False, sim_kw={"corr_groups": dict(forward.PLAN_GROUPS)})
    runner = PaperRunner.open(tmp_path, AppConfig(), market, clock, **kw)
    write_request(tmp_path, "resume", operator="sultan", reason="start the forward test")
    write_request(tmp_path, "trade", operator="sultan", reason="a manual idea", symbol="BTCUSDT", entry="80000",
                  stop="78000", target="86000", hours=72, invalidation="x")
    runner.tick()
    assert runner.sim.positions == {}
    refused = [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()
               if '"request_refused"' in x]
    assert refused and "only the strategy trades" in refused[0]["reason"]
    again = PaperRunner.open(tmp_path, AppConfig(), market, clock, **kw)
    assert again.sim.corr_groups == forward.PLAN_GROUPS and again.sim._group("XRPUSDT") == "XRPUSDT"


# ---------------------------------------------------------------- the commands
def fargs(tmp_path, **kw):
    a = dict(config="config/default.yaml", state_dir=str(tmp_path / "fw"), data_dir=str(tmp_path / "data"),
             operator="Sultan", confirm=forward.CONFIRM_PHRASE, interval=0, ticks=1)
    a.update(kw)
    return argparse.Namespace(**a)


def prepared_data(tmp_path):
    d = tmp_path / "data"
    (d / "screen").mkdir(parents=True)
    for s, cs in research().items():
        history.write_csv(d / f"{s}-4h.csv", cs)
        (d / f"{s}-rules.json").write_text(json.dumps(history.rules_to_info(rules((s,))[s])))
    (d / "screen" / "universe.json").write_text(json.dumps({"symbols": COINS, "groups": GROUPS}))


def test_start_run_and_status_end_to_end(tmp_path, fake_choose, monkeypatch, capsys):
    prepared_data(tmp_path)
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)
    out = io.StringIO()
    assert cli._forward_start(fargs(tmp_path), out=out) == 0
    assert "Settings (fixed for the whole test): threshold=0.5, atr_mult=3.6" in out.getvalue()
    assert cli._forward_status(fargs(tmp_path)) == 0
    assert "has not started yet" in capsys.readouterr().out
    market = FakeMarket()
    assert cli._forward_run(fargs(tmp_path), make_exchange=lambda **k: market, out=io.StringIO()) == 0
    fw = tmp_path / "fw"
    assert json.loads((fw / CLOCK_FILE).read_text()).keys() == {"H2b"}          # H2b is the account's strategy
    assert cli._forward_status(fargs(tmp_path)) == 0
    text = capsys.readouterr().out
    for must in (cli.FORWARD_LABEL, "settings threshold=0.5, atr_mult=3.6", "XRPUSDT (XRPUSDT)", "Day 0 of at least 365",
                 "closed trades 0 of at least 30", "Status: running", "Mode: ENTRIES_BLOCKED"):
        assert must in text, must
    assert "Paper review gate" not in text


def test_run_refuses_changed_code_and_status_says_so(tmp_path, fake_choose, monkeypatch, capsys):
    prepared_data(tmp_path)
    monkeypatch.setattr(universe, "available_memory_mb", lambda: None)
    assert cli._forward_start(fargs(tmp_path), out=io.StringIO()) == 0
    monkeypatch.setattr(forward, "strategy_sha256", lambda: "edited")
    assert cli._forward_run(fargs(tmp_path), make_exchange=lambda **k: FakeMarket(), out=io.StringIO()) == 2
    assert "restarts the test from zero" in capsys.readouterr().err
    assert not (tmp_path / "fw" / STATE_FILE).exists()                         # nothing ran
    cli._forward_status(fargs(tmp_path))
    assert "STOPPED:" in capsys.readouterr().out


def test_start_command_refuses_before_doing_anything(tmp_path, fake_choose, monkeypatch, capsys):
    prepared_data(tmp_path)
    monkeypatch.setattr(universe, "available_memory_mb", lambda: 50)
    assert cli._forward_start(fargs(tmp_path), out=io.StringIO()) == 2          # too little memory
    assert cli._forward_start(fargs(tmp_path, confirm="yes"), out=io.StringIO()) == 2
    assert "The forward test was not started" in capsys.readouterr().err
    assert fake_choose.calls == [] and not (tmp_path / "fw" / forward.FORWARD_FILE).exists()


def test_the_forward_commands_offer_no_manual_trade():
    with pytest.raises(SystemExit):
        cli.main(["forward", "trade", "--operator", "x", "--reason", "0123456789"])
    assert load_config("config/default.yaml").universe == ["BTCUSDT", "ETHUSDT"]   # paper trading keeps its coins


def test_start_works_with_the_real_settings_choice(tmp_path, monkeypatch):
    from bot.backtest import walkforward
    monkeypatch.setattr(walkforward, "GRID", (Dec(1),))           # one combination: fast
    cfg = AppConfig()
    wide = cfg.model_validate({**cfg.model_dump(), "universe": COINS})
    ft = forward.start(tmp_path, research(400), rules(COINS), wide, cfg, "Sultan", forward.CONFIRM_PHRASE, T0,
                       GROUPS, "abc")
    assert ft.params == {"threshold": "0.5", "atr_mult": "3"} and ft.train_score is None   # too little data to trade
