"""D1 daily trend pullback (Spot, long-only, signals only) and the `bot daily-signal` report. Made-up candles;
nothing here reads Binance or trades."""
import argparse
import ast
import io
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from bot import cli
from bot import daily_signal as ds
from bot.analysis.resample import DAY_MS
from bot.core.config import AppConfig
from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Candle
from bot.risk.models import MarketSnapshot
from bot.strategies import daily_pullback as dp
from bot.strategies.base import MAX_TUNABLE, Exit, OpenTrade
from tests.conftest import exchange_info_symbol
from tests.fakes import FakeMarket

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
H4 = 14_400_000
START = 19_000 * DAY_MS                      # a UTC midnight
N = 1000                                     # 4h candles of history (D1 needs 948)


def candles(closes, half_range):
    """4h candles: open = the previous close; high and low `half_range` beyond the body."""
    h, out = Dec(str(half_range)), []
    for i, c in enumerate(closes):
        c = Dec(str(c)).quantize(Dec("0.01"))
        o = out[-1].close if out else c
        t = START + i * H4
        out.append(Candle(t, o, (max(o, c) * (1 + h)).quantize(Dec("0.01")),
                          (min(o, c) * (1 - h)).quantize(Dec("0.01")), c, Dec(1), t + H4 - 1, Dec("5e7"), True))
    return out


def rising(end=80_000.0, n=N, step=0.0005):
    p0 = end / (1 + step) ** n
    return [p0 * (1 + step) ** i for i in range(n)]


def with_breakout(closes, half_range, jump=0.0005):
    """The last close a little above the previous candle's high (a rising candle's high is close x (1 + h))."""
    return closes + [closes[-1] * (1 + half_range) * (1 + jump)]


def at_close(cs):
    return (cs[-1].close_time + 1) / 1000


def setup(half_range=0.009, end=80_000.0):
    cs = candles(with_breakout(rising(end), half_range), half_range)
    return cs, at_close(cs)


S = dp.DailyPullback(["BTCUSDT", "ETHUSDT"])


# ---------------------------------------------------------------- contract and fixed values
def test_three_tunable_settings_and_the_fixed_rules():
    assert S.params() == {"trend_days": 50, "pullback_ema": 20, "stop_atr": Dec("1.5")}
    assert len(S.params()) <= MAX_TUNABLE
    assert S.with_params(stop_atr=Dec("2")).stop_atr == Dec("2") and S.stop_atr == Dec("1.5")
    assert (S.name, S.interval) == ("D1", "4h")
    assert (dp.SLOPE_DAYS, dp.PULLBACK_CANDLES, dp.ATR_N, dp.TAKE_PROFIT_R, dp.MAX_RANGE_ATR, dp.MAX_STOP_PCT,
            dp.HORIZON_HOURS) == (5, 6, 14, Dec("2"), Dec("2"), Dec("3"), 72)
    assert S.history_needed() == (3 * 50 + 5 + 3) * 6


def test_ema_is_seeded_with_the_average_and_ignores_older_history():
    vals = [Dec(x) for x in (1, 2, 3, 4, 5, 6)]
    assert dp.ema(vals, 2) == [Dec("1.5"), Dec("2.5"), Dec("3.5"), Dec("4.5"), Dec("5.5")]
    assert dp.ema([Dec(99)] * 50 + vals, 2) == dp.ema(vals, 2)          # fixed window: same candles, same value
    assert dp.ema(vals[:1], 2) == []


# ---------------------------------------------------------------- LONG
def test_long_on_pullback_and_resume_in_a_daily_uptrend():
    cs, now = setup()
    v = S.signal(cs, now)
    assert v.long, v.reason
    assert v.entry == cs[-1].close * Dec("1.001")                       # the bot's usual limit: last close + 0.10%
    assert v.stop == v.entry - Dec("1.5") * v.atr
    assert v.take_profit == v.entry + 2 * (v.entry - v.stop)
    assert "above the 50-day EMA" in v.reason and "touched the 20-EMA" in v.reason
    i = S.intent("BTCUSDT", v)
    assert i.side == "BUY" and i.horizon_hours == 72 and i.strategy == "D1"


def test_propose_reads_only_finished_candles_through_the_source():
    cs, now = setup()

    class Source:
        def __init__(self):
            self.asked = []

        def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
            self.asked.append((symbol, interval, limit))
            return cs[-limit:]
    src = Source()
    out = S.with_params().propose(src, now)
    assert [i.symbol for i in out] == ["BTCUSDT", "ETHUSDT"]
    assert src.asked == [("BTCUSDT", "4h", S.history_needed()), ("ETHUSDT", "4h", S.history_needed())]


# ---------------------------------------------------------------- NO TRADE
def test_no_trade_without_enough_history():
    cs, now = setup()
    assert S.signal(cs[-300:], now).reason == "not enough 4h history"


def test_no_trade_on_stale_data():
    cs, now = setup()
    v = S.signal(cs, now + 5 * 3600)
    assert not v.long and "stale" in v.reason


def test_no_trade_in_a_downtrend_and_never_a_short():
    falling = list(reversed(rising()))
    cs = candles(with_breakout(falling, 0.009), 0.009)
    v = S.signal(cs, at_close(cs))
    assert not v.long and "is not above the 50-day EMA" in v.reason
    assert S.intent("BTCUSDT", v) is None                               # Spot, long-only: nothing to propose


def test_no_trade_when_the_daily_ema_is_not_rising():
    base = list(reversed(rising(n=N - 12)))                              # a long fall ...
    jump = [base[-1] * 1.012 ** i for i in range(1, 13)]                # ... then two days sharply up
    cs = candles(with_breakout(base + jump, 0.009), 0.009)
    v = S.signal(cs, at_close(cs))
    assert not v.long and "is not rising" in v.reason


def test_no_trade_without_the_resume_candle():
    closes = rising()
    cs = candles(closes + [closes[-1] * 1.0005], 0.009)                  # a normal candle: no close above the high
    v = S.signal(cs, at_close(cs))
    assert not v.long and "no pullback-and-resume signal" in v.reason


def test_no_trade_without_a_pullback_to_the_ema():
    cs = candles(with_breakout(rising(), 0.0005, jump=0.0002), 0.0005)    # lows never reach the 20-EMA
    v = S.signal(cs, at_close(cs))
    assert not v.long and "no pullback-and-resume signal" in v.reason


def test_no_trade_on_a_spike_candle():
    closes = rising()
    cs = candles(closes + [closes[-1] * 1.06], 0.009)
    v = S.signal(cs, at_close(cs))
    assert not v.long and "a spike" in v.reason


def test_no_trade_when_the_stop_would_be_more_than_3_percent_away():
    cs, now = setup(half_range=0.013)
    v = S.signal(cs, now)
    assert not v.long and "more than 3% below the entry" in v.reason


def test_only_the_first_setup_of_the_day_counts():
    h = 0.009
    closes = with_breakout(rising(n=N + 2), h)                           # a breakout ...
    closes = closes + [closes[-1] * (1 + h) * 1.0005]                     # ... and another one right after
    cs = candles(closes, h)
    assert (cs[-1].open_time // DAY_MS) == (cs[-2].open_time // DAY_MS)  # same UTC day
    assert S._trigger(cs, len(cs) - 2)
    v = S.signal(cs, at_close(cs))
    assert not v.long and "already signalled earlier today" in v.reason


# ---------------------------------------------------------------- exits
def falling_source():
    cs = candles(list(reversed(rising())), 0.002)

    class Src:
        def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
            return cs[-limit:]
    return Src(), cs


def test_sells_at_the_first_4h_close_after_midnight_when_the_trend_ends():
    src, cs = falling_source()
    trade = [OpenTrade("BTCUSDT", Dec(100), Dec(95), 0.0)]
    midnight = (cs[-1].close_time + 1) // DAY_MS * DAY_MS / 1000
    out = S.manage(src, midnight, trade)
    assert out == [Exit("BTCUSDT", "the daily close fell below its 50-day EMA")]
    assert S.manage(src, midnight + 8 * 3600, trade) == []              # only once a day
    up_src = type("U", (), {"closed_candles": lambda self, *a, limit=500, **k: setup()[0][-limit:]})()
    assert S.manage(up_src, midnight, trade) == []


# ---------------------------------------------------------------- the report and the risk engine
class Data:
    """closed_candles, rules and a good market snapshot at the last close (no network)."""

    def __init__(self, by_symbol, fail=()):
        self.by_symbol, self.fail = by_symbol, set(fail)

    def closed_candles(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        if symbol in self.fail:
            raise RuntimeError("klines down")
        return self.by_symbol[symbol][-limit:]

    def rules(self, symbol):
        return parse_symbol(exchange_info_symbol(symbol))

    def snapshot(self, symbol):
        ask = self.by_symbol[symbol][-1].close
        return MarketSnapshot(best_bid=ask - Dec("0.01"), best_ask=ask, quote_volume_24h=Dec("1e10"),
                              ask_depth_quote=Dec("1e9"), data_age_s=Dec(0), clock_skew_ms=0, reference_price=ask)


def test_long_is_sized_by_the_risk_engine_at_0_25_percent_risk():
    cs, now = setup()
    [c] = ds.report(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(1000), now)
    assert c.long, c.reason
    d = c.decision
    assert d.accepted and d.planned_risk <= Dec("2.5") and d.qty > 0          # R-01: 0.25% of 1,000 USDT
    assert d.reward_risk >= Dec("1.5")
    text = ds.render([c], Dec(1000), Dec(0), now)
    for part in ("BTCUSDT: LONG", "Entry (limit)", "Stop-loss", "Take-profit", "Size", "Reward/risk", "Reason"):
        assert part in text


def test_engine_rejection_is_shown_as_no_trade_with_its_reason():
    # ETH's 1.0% stop-slippage allowance: a 2R target cannot reach 1.5 after costs with a stop under 3%.
    cs, now = setup(end=2500.0)
    [c] = ds.report(dp.DailyPullback(["ETHUSDT"]), Data({"ETHUSDT": cs}), AppConfig(), Dec(1000), now)
    assert c.verdict.long and not c.long
    assert "risk engine says no (REJ_REWARD_RISK)" in c.reason
    assert "ETHUSDT: NO TRADE" in ds.render([c], Dec(1000), Dec(0), now)


def test_a_tight_stop_on_btc_is_rejected_by_the_engine_too():
    cs, now = setup(half_range=0.006)                                    # stop about 1.9% below: under 2.72%
    [c] = ds.report(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(1000), now)
    assert c.verdict.long and not c.long and "REWARD_RISK" in c.reason


def test_the_daily_loss_limit_blocks_a_long():
    cs, now = setup()
    [c] = ds.report(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}), AppConfig(), Dec(1000), now,
                    loss_today=Dec(15))                                   # R-02: 1.5% of 1,000 USDT already lost
    assert not c.long and "risk engine says no" in c.reason


def test_market_data_failure_is_no_trade():
    cs, now = setup()
    [c] = ds.report(dp.DailyPullback(["BTCUSDT"]), Data({"BTCUSDT": cs}, fail={"BTCUSDT"}), AppConfig(),
                    Dec(1000), now)
    assert not c.long and "market data unavailable" in c.reason


def test_record_appends_one_line_per_coin(tmp_path):
    cs, now = setup()
    sigs = ds.report(dp.DailyPullback(["BTCUSDT", "ETHUSDT"]), Data({"BTCUSDT": cs, "ETHUSDT": cs}), AppConfig(),
                     Dec(1000), now)
    path = ds.record(tmp_path / "log", sigs, now, Dec(1000))
    ds.record(tmp_path / "log", sigs, now, Dec(1000))
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 4 and [r["symbol"] for r in rows[:2]] == ["BTCUSDT", "ETHUSDT"]
    assert rows[0]["signal"] == "LONG" and {"entry", "stop", "take_profit", "qty", "reward_risk"} <= set(rows[0])
    assert list((tmp_path / "log").iterdir()) == [path]


# ---------------------------------------------------------------- CLI
def args(tmp_path, **kw):
    a = dict(config="config/default.yaml", equity="1000", loss_today="0", fee="0.001",
             log_dir=str(tmp_path / "sig"), no_log=False)
    a.update(kw)
    return argparse.Namespace(**a)


def test_cli_prints_and_logs_and_no_log_writes_nothing(tmp_path):
    out = io.StringIO()
    assert cli._daily_signal(args(tmp_path), make_exchange=lambda environment: FakeMarket(), out=out) == 0
    assert "BTCUSDT: NO TRADE" in out.getvalue() and "SIMULATED" in out.getvalue()
    assert (tmp_path / "sig" / ds.LOG_FILE).exists()
    out2 = io.StringIO()
    assert cli._daily_signal(args(tmp_path / "x", no_log=True), make_exchange=lambda environment: FakeMarket(),
                             out=out2) == 0
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("kw", [{"equity": "0"}, {"equity": "abc"}, {"loss_today": "-1"}, {"fee": "1"}])
def test_cli_refuses_bad_inputs(tmp_path, kw):
    assert cli._daily_signal(args(tmp_path, **kw), make_exchange=lambda environment: FakeMarket(),
                             out=io.StringIO()) == 2


def test_cli_parser_has_the_command():
    with pytest.raises(SystemExit):
        cli.main(["daily-signal", "--help"])


# ---------------------------------------------------------------- isolation
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
    return out


def test_d1_cannot_reach_orders_credentials_or_h2b():
    for f in (SRC / "daily_signal.py", SRC / "strategies" / "daily_pullback.py"):
        mods = _imports(f)
        for bad in ("bot.execution", "bot.exchange.trading", "bot.exchange.credentials", "bot.exchange.binance",
                    "bot.forward", "bot.paper", "bot.strategies.h2b_blend"):
            assert not any(m == bad or m.startswith(bad + ".") for m in mods), (f, bad)


def test_d1_is_not_wired_into_any_runner_or_backtest():
    users = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
             if any("daily_pullback" in m or m == "bot.daily_signal" for m in _imports(p))]
    assert sorted(users) == ["cli.py", "daily_signal.py", "trade_planner.py"]    # the planner is read-only too
