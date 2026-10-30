"""Future trade plans for H1-DailyTrader (`bot.h1dt_plans`, `bot plan --strategy H1DT`). Made-up candles and
illustrative exchange rules; nothing here reads Binance or trades."""
import argparse
import ast
import io
from dataclasses import replace
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot import h1dt_plans as hp
from bot.core.config import AppConfig
from bot.core.money import plain
from bot.daily_signal import account
from bot.strategies.base import entry_limit
from bot.strategies.daily_trader import DailyTrader
from tests.fakes import FakeMarket
from tests.unit.test_daily_pullback import Data
from tests.unit.test_daily_trader import breakout, ranging

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"


def at(cs):
    return (cs[-1].close_time + 1) / 1000


def plan(setup, cs, equity=1000, symbol="BTCUSDT", held=Dec(0), data=None, **kw):
    return hp.plan_setup(DailyTrader([symbol], setup, **kw), data or Data({symbol: cs}), symbol, AppConfig(),
                         account(Dec(equity), Dec(0), Dec("0.001")), at(cs), held)


def test_a_breakout_on_the_last_candle_is_ready_and_eligible_on_a_big_enough_account():
    cs = breakout()
    p = plan("BRK", cs)
    assert p.status == hp.READY and p.pilot.accepted and p.check.accepted, p.reason
    assert p.check.reward_risk >= Dec("1.5") and p.stop < p.entry < p.target
    assert p.order.startswith("yes") and "approval" in p.order and p.watch_until == at(cs) + 3600
    assert p.fees > 0 and p.slippage > 0 and p.invalidation


def test_ready_but_the_pilot_is_too_small_or_the_coin_is_already_held():
    cs = breakout()
    small = plan("BRK", cs, equity=20)
    assert small.status == hp.READY and not small.pilot.accepted and small.order.startswith("no: the pilot")
    held = plan("BRK", cs, held=Dec("0.001"))
    assert held.status == hp.READY and "already holds 0.001 BTC (shared with H2b)" in held.order


def test_without_the_trigger_it_watches_the_level_the_next_close_must_break():
    cs = breakout()[:-1]
    p = plan("BRK", cs)
    level = max(c.high for c in cs[-24:])
    assert p.status == hp.WATCHING and any(f"a 1h close above {plain(level)}" in n for n in p.needs)
    assert p.entry == entry_limit(level).quantize(Dec("0.01"), rounding="ROUND_FLOOR")
    assert p.order == "no: not triggered" and p.check.accepted


def test_a_plan_the_engine_refuses_at_any_size_is_not_tradeable():
    # ETH: a 1.0% stop-slippage allowance; a stop about 1% away and a 3R target cannot reach 1.5 after costs
    cs = breakout()
    p = plan("BRK", cs, symbol="ETHUSDT", data=Data({"ETHUSDT": cs}))
    assert p.status == hp.NOT_TRADEABLE and "at any size (REJ_REWARD_RISK)" in p.reason


def test_no_chasing_a_price_above_the_entry_limit():
    cs = breakout()

    class Up(Data):
        def snapshot(self, symbol):
            s = super().snapshot(symbol)
            return replace(s, best_ask=s.best_ask * Dec("1.01"), best_bid=s.best_bid * Dec("1.01"))
    p = plan("BRK", cs, data=Up({"BTCUSDT": cs}))
    assert p.status == hp.NOT_TRADEABLE and "no chasing" in p.reason


def test_range_watches_the_buy_zone_and_continuation_lists_its_conditions():
    r = plan("RNG", ranging(), stop_atr=Dec("0.5"), target_r=Dec("1.5"))
    assert r.status in (hp.WATCHING, hp.NOT_TRADEABLE) and any("dipping to" in n for n in r.needs)
    c = plan("CONT", breakout()[:-1])
    assert any("1h close above" in n for n in c.needs) and c.entry is not None


def test_no_data_is_reported_not_guessed():
    p = plan("BRK", breakout()[:50])
    assert p.status == hp.NO_DATA and p.entry is None


def test_plans_cover_every_coin_and_setup_and_are_capped_at_the_pilot():
    cs = breakout()
    data = Data({"BTCUSDT": cs, "ETHUSDT": cs})
    ps = hp.plans(data, AppConfig(), Dec(20), at(cs))
    assert [(p.symbol, p.setup) for p in ps] == [(s, k) for s in ("BTCUSDT", "ETHUSDT") for k in ("BRK", "CONT", "RNG")]
    with pytest.raises(ValueError):
        hp.plans(data, AppConfig(), Dec("20.01"), at(cs))
    text = hp.render(ps, Dec(20), at(cs))
    for part in ("Setup", "Status", "Entry", "Stop", "Target", "Net R:R", "Min size", "Max loss", "Watch until",
                 "Exchange order", "Invalid if", "Fees (est.)", "Projections and plans, not orders"):
        assert part in text, part


def args(tmp_path, **kw):
    a = dict(config="config/default.yaml", equity="20", loss_today="0", fee="0.001", from_account=False,
             log_dir=str(tmp_path / "plans"), no_log=True, watch=False, strategy="H1DT")
    a.update(kw)
    return argparse.Namespace(**a)


def test_cli_prints_h1dt_plans(tmp_path):
    out = io.StringIO()
    assert cli._plan(args(tmp_path), make_exchange=lambda environment: FakeMarket(), out=out) == 0
    assert "H1-DailyTrader future setups" in out.getvalue() and "NO DATA" in out.getvalue()


def test_cli_parser_takes_the_strategy():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--strategy", "H1DT", "--help"])
    with pytest.raises(SystemExit):
        cli.main(["plan", "--strategy", "H9"])


def test_the_plans_cannot_reach_orders_credentials_or_h2b():
    tree = ast.parse((SRC / "h1dt_plans.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.credentials", "bot.exchange.binance",
                "bot.forward", "bot.paper", "bot.strategies.h2b_blend"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad


def test_a_range_target_above_the_range_top_is_refused_by_the_setups_own_rule():
    # the same rule as DailyTrader.trigger(): the target must lie inside the range
    p = plan("RNG", ranging(), target_r=Dec("20"))
    assert p.status == hp.NOT_TRADEABLE and "above the range top" in p.reason and p.check is None
    assert p.target is not None and p.order == ""


def test_the_footer_says_the_strategy_was_rejected():
    cs = breakout()
    text = hp.render(hp.plans(Data({"BTCUSDT": cs, "ETHUSDT": cs}), AppConfig(), Dec(20), at(cs)), Dec(20), at(cs))
    assert "REJECTED by its walk-forward study" in text and "do not trade them" in text
