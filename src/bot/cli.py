"""Command-line entry point.

    bot check-config [--config config/default.yaml]
    bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 [--symbol BTCUSDT]
    bot check-binance [--public-only] [--details]
    bot paper run | status | resume | block | trade      (paper trading, simulated money only)
    bot backtest fetch | baselines | run                  (history, baselines, strategies H1-H3)

`try-trade` runs a proposed trade through the real risk engine with example
market data and prints the decision in plain language. It sends nothing to Binance.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

from bot.core.config import load_config
from bot.exchange.filters import parse_symbol
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, MarketSnapshot, Mode, TradeIntent

# Example rules shaped like GET /api/v3/exchangeInfo. Live code loads the real ones.
_EXAMPLE_RULES = {
    "symbol": "", "status": "TRADING", "baseAsset": "", "quoteAsset": "USDT",
    "ocoAllowed": True, "otoAllowed": True, "opoAllowed": True,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True, "maxNotional": "9000000"},
    ],
}


def _check_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    r = cfg.risk
    print(f"Config OK: environment={cfg.environment.value}, live_trading={cfg.live_trading}")
    print(f"Universe: {', '.join(cfg.universe)}")
    print(f"Risk per trade {r.risk_per_trade_pct}% | daily limit {r.daily_loss_pct}% | weekly {r.weekly_loss_pct}% | "
          f"max positions {r.max_open_positions} | drawdown breaker {r.drawdown_breaker_pct}%")
    return 0


def _try_trade(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    D = Decimal
    equity = D(args.equity)
    rules = parse_symbol({**_EXAMPLE_RULES, "symbol": args.symbol, "baseAsset": args.symbol[:-4]})
    intent = TradeIntent(symbol=args.symbol, side="BUY", entry_price=D(args.entry), stop_price=D(args.stop),
                         take_profit_price=D(args.target), strategy="manual-test", horizon_hours=72,
                         invalidation="manual test trade")
    market = MarketSnapshot(best_bid=D(args.entry) * D("0.9999"), best_ask=D(args.entry),
                            quote_volume_24h=D("1000000000"), ask_depth_quote=D("5000000"),
                            data_age_s=D("1"), clock_skew_ms=10, reference_price=D(args.entry))
    state = AccountState(mode=Mode.RUNNING, equity=equity, free_quote=equity, day_start_equity=equity,
                         week_start_equity=equity, daily_pnl=-D(args.loss_today), weekly_pnl=-D(args.loss_today),
                         drawdown_pct=D("0"), taker_fee_rate=D(args.fee))
    d = evaluate(intent, state, market, rules, cfg, corr_group="majors")
    if d.accepted:
        print("ACCEPTED")
        print(d.reason)
    else:
        print(f"REJECTED ({d.code.value})")
        print(d.reason)
    print("\nExample market data and exchange rules were used; nothing was sent to Binance.")
    return 0 if d.accepted else 1


def _binance_host_for(environment: str) -> str:
    # Paper trading and simulation read real market data from the live exchange (read-only).
    return {"sim": "live", "paper": "live", "live": "live", "testnet": "testnet"}.get(environment, environment)


def _check_binance(args: argparse.Namespace, make_exchange=None, load_creds=None) -> int:
    """Exit codes: 0 all checks passed, 2 setup problem, 3 unsafe key, 4 Binance error."""
    from bot.exchange.binance import BinanceReadOnly, ExchangeError
    from bot.exchange.credentials import CredentialsError, load_credentials

    cfg = load_config(args.config)
    host = _binance_host_for(cfg.environment.value)
    make_exchange = make_exchange or BinanceReadOnly
    load_creds = load_creds or load_credentials
    ok = True

    print(f"Binance read-only check ({host} exchange, bot environment: {cfg.environment.value})")
    creds = None
    if not args.public_only:
        try:
            creds = load_creds()
        except CredentialsError as e:
            print(f"  API key ........ NOT READY: {e}")
            return 2
    try:
        ex = make_exchange(environment=host, credentials=creds)
        skew = ex.clock_skew_ms()
        skew_ok = abs(skew) <= cfg.risk.max_clock_skew_ms
        ok &= skew_ok
        print(f"  Clock .......... {'OK' if skew_ok else 'PROBLEM'}: {skew:+d} ms vs Binance (limit {cfg.risk.max_clock_skew_ms} ms)")

        if creds is not None:
            perms = ex.key_permissions()
            if perms.safe:
                print("  API key ........ SAFE: reading only, restricted to trusted IPs")
            else:
                print("  API key ........ STOP, this key has permissions a read-only bot must not have:")
                for p in perms.problems:
                    print(f"                   - {p}")
                print("                   Fix it in Binance > API Management > Edit restrictions, then run this again.")
                return 3
            for w in perms.warnings:
                print(f"  API key note ... {w}")

        rules = ex.symbol_rules(cfg.universe)
        books = ex.books(cfg.universe)
        vols = ex.quote_volume_24h(cfg.universe)
        for sym in cfg.universe:
            r, b = rules[sym], books[sym]
            ready = r.is_trading and r.oco_allowed
            ok &= ready
            print(f"  {sym:<14} {'OK' if ready else 'PROBLEM'}: {r.status}, bid {b.bid.normalize()} / ask {b.ask.normalize()}, "
                  f"spread {b.spread_pct:.3f}%, 24h volume {vols[sym] / 1_000_000:,.0f}M USDT, "
                  f"OCO {'yes' if r.oco_allowed else 'NO'}, OPO {'yes' if r.opo_allowed else 'no'}")
            if creds is not None:
                fee = ex.taker_fee(sym)
                print(f"  {'':<14} taker fee {fee * 100:.3f}%, min order {r.min_notional.normalize()} USDT, "
                      f"step {r.step_size.normalize()}" + (f", MAX_ASSET {r.max_asset_qty}" if r.max_asset_qty else ""))

        if creds is not None:
            bals = ex.balances()
            equity, unvalued = ex.value_balances(bals)
            print(f"  Balances ....... {len(bals)} assets, about {equity:,.2f} USDT valued at best bid")
            if args.details:
                for b in sorted(bals, key=lambda x: x.asset):
                    print(f"                   {b.asset:<8} free {b.free.normalize()}  locked {b.locked.normalize()}")
            if unvalued:
                print(f"  Not valued ..... {', '.join(sorted(unvalued))} (no {cfg.quote_asset} market; not counted, the bot will not trade them)")
    except ExchangeError as e:
        print(f"  Binance ........ ERROR: {e}")
        return 4

    print("Result: " + ("ALL CHECKS PASSED. Read-only; no orders can be sent." if ok else "SOME CHECKS FAILED (see PROBLEM above)."))
    return 0 if ok else 4


DEFAULT_PAPER_DIR = "~/paper"
PAPER_GATE_WEEKS, PAPER_GATE_TRADES, PAPER_GATE_PROFITABLE = 8, 30, 5


def _paper_dir(args: argparse.Namespace) -> Path:
    return Path(os.path.expanduser(args.state_dir))


def _paper_run(args: argparse.Namespace, make_exchange=None, out=None) -> int:
    """Run paper trading until stopped. Reads live prices (no API key needed); moves only simulated money."""
    from bot.exchange.binance import BinanceReadOnly
    from bot.paper.runner import PaperRunner, RunnerLock, RunnerLocked
    from bot.paper.store import StateError

    cfg = load_config(args.config)
    if cfg.environment.value not in ("paper", "sim") or cfg.live_trading:
        print(f"Error: paper trading needs environment paper or sim (config says {cfg.environment.value})",
              file=sys.stderr)
        return 2
    state_dir = _paper_dir(args)
    market = (make_exchange or BinanceReadOnly)(environment=_binance_host_for(cfg.environment.value))
    try:
        with RunnerLock(state_dir):
            runner = PaperRunner.open(state_dir, cfg, market, out=out or sys.stdout)

            def stop(signum, frame):
                runner.stop_requested = True

            old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
            try:
                runner.run(interval_s=args.interval, max_ticks=args.ticks)
            finally:
                for s, h in old.items():
                    signal.signal(s, h)
    except (RunnerLocked, StateError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


def _runner_active(state_dir: Path) -> bool:
    from bot.paper.runner import LOCK_FILE, RunnerLock, RunnerLocked

    if not (state_dir / LOCK_FILE).exists():
        return False
    try:
        with RunnerLock(state_dir):
            return False
    except RunnerLocked:
        return True


def _paper_status(args: argparse.Namespace) -> int:
    import json

    from bot.core.money import fmt
    from bot.paper import store
    from bot.paper.runner import STATE_FILE

    cfg = load_config(args.config)
    state_dir = _paper_dir(args)
    path = state_dir / STATE_FILE
    if not path.exists():
        print(f"No paper account yet in {state_dir}. Start one with: bot paper run")
        return 1
    data = json.loads(path.read_text())
    sim = store.from_dict(data, cfg, market=None, clock=lambda: data["saved_at"])
    D = Decimal
    equity = sim.equity()
    start = sim.starting_quote
    trades = sim.trades
    wins = sum(1 for t in trades if t.pnl > 0)
    fees = sum((f.fee for f in sim.fills), D(0))
    days = (data["saved_at"] - sim.created_at) / 86400
    saved = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(data["saved_at"]))
    print(f"Paper account in {state_dir} (simulated money only)")
    print(f"Runner: {'running' if _runner_active(state_dir) else 'NOT running'}; last saved {saved}")
    print(f"Mode: {sim.modes.mode.value}" + (f"; active breakers: {', '.join(sorted(sim.modes.active_breaches))}"
                                              if sim.modes.active_breaches else ""))
    print(f"Equity {equity:.2f} USDT (started {start:.2f}, {(equity - start) / start * 100:+.2f}%), "
          f"cash {sim.quote:.2f}, drawdown {sim.index.drawdown_pct:.2f}%")
    print(f"Today {sim.periods.daily_pnl(equity):+.2f} USDT, this week {sim.periods.weekly_pnl(equity):+.2f} USDT; "
          f"fees paid {fees:.2f} USDT")
    if sim.positions:
        print("Open positions:")
        for p in sim.positions.values():
            bid = sim._bids[p.symbol]
            print(f"  {p.symbol} {fmt(p.qty)} bought {fmt(p.entry_price)}, now {fmt(bid)}, stop {fmt(p.stop_price)}, "
                  f"target {fmt(p.take_profit_price)}, unrealised {p.qty * bid - p.qty * p.entry_price - p.entry_fee:+.2f}")
    else:
        print("Open positions: none")
    print(f"Closed trades: {len(trades)} ({wins} profitable), total P&L "
          f"{sum((t.pnl for t in trades), D(0)):+.2f} USDT")
    print(f"Paper review gate: {days:.1f} of {PAPER_GATE_WEEKS * 7} days, {len(trades)} of {PAPER_GATE_TRADES} trades, "
          f"{wins} of {PAPER_GATE_PROFITABLE} profitable (rule violations are reviewed separately)")
    return 0


def _paper_request(kind: str):
    def handler(args: argparse.Namespace) -> int:
        from bot.paper.runner import write_request

        if not args.operator.strip() or len(args.reason.strip()) < 10:
            print("Error: give your name with --operator and a reason of 10+ characters with --reason",
                  file=sys.stderr)
            return 2
        payload = {"operator": args.operator, "reason": args.reason}
        if kind == "trade":
            payload.update(symbol=args.symbol, entry=args.entry, stop=args.stop, target=args.target,
                           hours=args.hours, invalidation=args.reason, strategy="manual")
        state_dir = _paper_dir(args)
        write_request(state_dir, kind, **payload)
        print(f"Request '{kind}' queued for the paper runner in {state_dir}.")
        if kind == "trade":
            print("It still goes through the risk engine; see the result in: bot paper status, or journal.jsonl")
        if not _runner_active(state_dir):
            print("Note: the paper runner is not running; the request waits until it starts.")
        return 0
    return handler


DEFAULT_DATA_DIR = "~/backtest-data"
DEFAULT_REPORTS_DIR = "~/backtests"


def _backtest_fetch(args: argparse.Namespace, make_exchange=None) -> int:
    """Download price history through the read-only adapter (public data, no API key)."""
    from bot.backtest import history
    from bot.exchange.binance import BinanceReadOnly

    cfg = load_config(args.config)
    venue = (make_exchange or BinanceReadOnly)(environment="live")
    data_dir = Path(os.path.expanduser(args.data_dir))
    ok = True
    for symbol in args.symbols or cfg.universe:
        try:
            res = history.fetch(venue, data_dir, symbol, args.interval)
            print(f"{symbol} {args.interval}: {history.describe(res, args.interval)}")
            def when(ms):
                return time.strftime('%Y-%m-%d %H:%M', time.gmtime(ms / 1000))
            for a, b in res.gaps[:5]:
                print(f"    no candles from {when(a)} to {when(b)} UTC")
            if len(res.gaps) > 5:
                print(f"    ... and {len(res.gaps) - 5} more gaps")
            for a, b in res.irregular[:5]:
                print(f"    irregular candle {when(a)} to {when(b + 1)} UTC ({(b + 1 - a) / 3_600_000:.2f} hours)")
            if len(res.irregular) > 5:
                print(f"    ... and {len(res.irregular) - 5} more irregular candles")
        except Exception as e:
            ok = False
            print(f"{symbol} {args.interval}: FAILED: {e}", file=sys.stderr)
    print(f"Saved in {data_dir}. The holdout (from 2025-10-01) is stored but stays locked.")
    return 0 if ok else 4


def _backtest_baselines(args: argparse.Namespace, out=None) -> int:
    from bot.backtest import history, report

    cfg = load_config(args.config)
    data_dir = Path(os.path.expanduser(args.data_dir))
    out = out or sys.stdout
    try:
        candles = {s: history.load(data_dir, s, args.interval) for s in cfg.universe}
        rules = {s: history.load_rules(data_dir, s) for s in cfg.universe}
    except history.DataError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    def progress(i, n):
        if i == n or i % 20 == 0:
            out.write(f"  random entries: {i} of {n} runs\n")
            out.flush()

    out.write(f"Running baselines on {', '.join(cfg.universe)} ({args.interval}), {args.seeds} random runs...\n")
    results = report.run_baselines(candles, rules, cfg, seeds=args.seeds, progress=progress)
    meta = report.provenance(cfg, candles, report.CostModel())
    folder = report.write(Path(os.path.expanduser(args.reports_dir)), "baselines", results, meta)
    out.write(report.render(results, meta))
    out.write(f"\nReport saved in {folder}\n")
    return 0


STRATEGIES = ("H1", "H2", "H3")


def _make_strategy(name: str, symbols: list[str]):
    from bot.strategies.h1_trend import TrendBreakout
    from bot.strategies.h2_momentum import TimeSeriesMomentum
    from bot.strategies.h3_mean_reversion import RangeMeanReversion

    return {"H1": TrendBreakout, "H2": TimeSeriesMomentum, "H3": RangeMeanReversion}[name](symbols)


def _backtest_run(args: argparse.Namespace, out=None) -> int:
    """Walk-forward and robustness tests of H1-H3 on the research period. The holdout stays locked."""
    from bot.backtest import history, report, selection

    cfg = load_config(args.config)
    data_dir = Path(os.path.expanduser(args.data_dir))
    out = out or sys.stdout
    try:
        candles = {s: history.load(data_dir, s, args.interval) for s in cfg.universe}
        rules = {s: history.load_rules(data_dir, s) for s in cfg.universe}
    except history.DataError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    def say(msg):
        out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
        out.flush()

    cost = report.CostModel()
    meta = report.provenance(cfg, candles, cost)
    verdicts = []
    for name in args.strategy:
        say(f"{name}: starting (27 setting combinations x 8 windows, then robustness checks)")
        ev = selection.evaluate(_make_strategy(name, list(cfg.universe)), candles, rules, cfg, cost,
                                random_runs=args.random_runs, progress=say)
        text = selection.render(ev, meta, cfg)
        folder = report.write_files(Path(os.path.expanduser(args.reports_dir)), f"strategy-{name}", text,
                                    {"meta": meta, **selection.summary(ev)})
        out.write("\n" + text + f"\nReport saved in {folder}\n\n")
        verdicts.append((name, ev.passes_walk_forward, folder))
    out.write("Summary (walk-forward part of the selection rule; the holdout is step 3):\n")
    for name, ok, folder in verdicts:
        out.write(f"  {name}: {'passes, holdout pending' if ok else 'does not qualify'}  ({folder})\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bot")
    p.add_argument("--config", default="config/default.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    t = sub.add_parser("try-trade")
    t.add_argument("--symbol", default="BTCUSDT")
    t.add_argument("--equity", required=True)
    t.add_argument("--entry", required=True)
    t.add_argument("--stop", required=True)
    t.add_argument("--target", required=True)
    t.add_argument("--loss-today", default="0", help="USDT already lost today")
    t.add_argument("--fee", default="0.001", help="taker fee as a fraction (0.001 = 0.1%%)")
    c = sub.add_parser("check-binance", help="read-only connection check: key permissions, prices, rules, balances")
    c.add_argument("--public-only", action="store_true", help="skip everything that needs the API key")
    c.add_argument("--details", action="store_true", help="list each balance instead of the total only")
    pp = sub.add_parser("paper", help="paper trading on live prices with simulated money")
    psub = pp.add_subparsers(dest="paper_cmd", required=True)
    pr = psub.add_parser("run", help="run paper trading until stopped (used by the bot-paper service)")
    pr.add_argument("--interval", type=float, default=5.0, help="seconds between price checks")
    pr.add_argument("--ticks", type=int, default=None, help=argparse.SUPPRESS)
    psub.add_parser("status", help="balances, positions, trades and progress toward the paper review")
    for name, help_ in (("resume", "allow new entries (refused while a breaker is active)"),
                        ("block", "block new entries; exits keep working"),
                        ("trade", "submit a manual trade idea; it goes through the risk engine")):
        q = psub.add_parser(name, help=help_)
        q.add_argument("--operator", required=True)
        q.add_argument("--reason", required=True, help="10+ characters; for trade, the invalidation condition")
        if name == "trade":
            q.add_argument("--symbol", default="BTCUSDT")
            q.add_argument("--entry", required=True)
            q.add_argument("--stop", required=True)
            q.add_argument("--target", required=True)
            q.add_argument("--hours", type=int, default=72, help="time stop: sell after this many hours")
    for q in psub.choices.values():
        q.add_argument("--state-dir", default=DEFAULT_PAPER_DIR, help="where the paper account is kept")
    bp = sub.add_parser("backtest", help="price history and backtests (research period only; holdout locked)")
    bsub = bp.add_subparsers(dest="backtest_cmd", required=True)
    bf = bsub.add_parser("fetch", help="download price history through the read-only adapter")
    bf.add_argument("--symbols", nargs="*", default=None, help="default: the config universe")
    bb = bsub.add_parser("baselines", help="cash, buy-and-hold, 50/50 and random entries on the research period")
    bb.add_argument("--seeds", type=int, default=200, help="number of random-entry runs")
    bb.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    br = bsub.add_parser("run", help="walk-forward and robustness tests of strategies H1-H3 (research period)")
    br.add_argument("--strategy", nargs="+", choices=STRATEGIES, default=list(STRATEGIES))
    br.add_argument("--random-runs", type=int, default=50, help="random-entry runs with the same exits")
    br.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    for q in bsub.choices.values():
        q.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
        q.add_argument("--interval", default="4h", choices=["4h"])
    args = p.parse_args(argv)
    try:
        if args.cmd == "backtest":
            return {"fetch": _backtest_fetch, "baselines": _backtest_baselines,
                    "run": _backtest_run}[args.backtest_cmd](args)
        if args.cmd == "paper":
            return {"run": _paper_run, "status": _paper_status, "resume": _paper_request("resume"),
                    "block": _paper_request("block"), "trade": _paper_request("trade")}[args.paper_cmd](args)
        return {"check-config": _check_config, "try-trade": _try_trade,
                "check-binance": _check_binance}[args.cmd](args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
