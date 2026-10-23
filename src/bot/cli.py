"""Command-line entry point.

    bot check-config [--config config/default.yaml]
    bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 [--symbol BTCUSDT]
    bot check-binance [--public-only] [--details]
    bot check-trading [--symbol BTCUSDT]                  (testnet trading key and a test order; places nothing)
    bot testnet-roundtrip [--symbol BTCUSDT]              (one protected trade on the testnet, fake money)
    bot paper run | status | resume | block | trade      (paper trading, simulated money only)
    bot forward start | run | status | report | resume | block   (the H2b forward test, simulated money)
    bot backtest screen | fetch | baselines | run         (coin screen, history, baselines, strategies H1-H3, H2b)
    bot backtest holdout                                  (the one-time holdout test; Sultan only)

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


def _check_trading(args: argparse.Namespace, make_trader=None, make_reader=None, load_creds=None) -> int:
    """Testnet trading key check (module 5a): account, permissions, clock and a TEST order
    (Binance validates it without trading). No order is placed.
    Exit codes: 0 passed, 2 setup problem, 3 unsafe key, 4 Binance error or refused test order."""
    from bot.core.ids import client_order_id, new_intent_id
    from bot.core.money import D, floor_price, floor_to_step
    from bot.exchange.binance import BinanceReadOnly, ExchangeError
    from bot.exchange.credentials import CredentialsError, load_credentials
    from bot.exchange.trading import BinanceTrader, OrderRejected, OrderRequest, check_trading_account

    load_creds = load_creds or (lambda: load_credentials(prefix="BINANCE_TESTNET"))
    print("Binance TESTNET trading check (testnet.binance.vision; fake money; no order is placed)")
    try:
        creds = load_creds()
    except CredentialsError as e:
        print(f"  Testnet key .... NOT READY: {e}")
        return 2
    try:
        trader = (make_trader or BinanceTrader)(environment="testnet", credentials=creds)
        reader = (make_reader or BinanceReadOnly)(environment="testnet")
        skew = reader.clock_skew_ms()
        print(f"  Clock .......... {'OK' if abs(skew) <= 500 else 'PROBLEM'}: {skew:+d} ms vs the testnet")
        info = trader.account()
        check = check_trading_account(info, "testnet")
        if not check.safe:
            print("  Testnet key .... STOP:")
            for p in check.problems:
                print(f"                   - {p}")
            return 3
        print(f"  Testnet key .... OK: trading enabled, account {info.account_type}")
        for n in check.notes:
            print(f"  Note ........... {n}")
        usdt = next((b.total for b in info.balances if b.asset == "USDT"), D(0))
        print(f"  Balances ....... {len(info.balances)} assets, {usdt:,.2f} USDT (testnet, not real)")
        rules = reader.symbol_rules([args.symbol])[args.symbol]
        bid = reader.books([args.symbol])[args.symbol].bid
        price = floor_price(bid * D("0.9"), rules.tick_size)               # far below the market: test only
        qty = floor_to_step(max(rules.min_notional, D(5)) * 2 / price, rules.step_size)
        req = OrderRequest(args.symbol, "BUY", "LIMIT", qty, client_order_id(new_intent_id(), "entry"),
                           price=price, time_in_force="GTC")
        trader.test_order(req, rules)
        print(f"  Test order ..... OK: Binance accepted a test LIMIT BUY {qty.normalize()} {args.symbol} at "
              f"{price.normalize()} (validated only, not placed)")
        print(f"  Open orders .... {len(trader.open_orders(args.symbol))} on {args.symbol}")
    except OrderRejected as e:
        print(f"  Test order ..... REFUSED: {e}")
        return 4
    except ExchangeError as e:
        print(f"  Binance ........ ERROR: {e}")
        return 4
    print("Result: TESTNET TRADING KEY WORKS. Nothing was bought or sold.")
    return 0


DEFAULT_TESTNET_DIR = "~/testnet"


def _testnet_roundtrip(args: argparse.Namespace, make_trader=None, make_reader=None, load_creds=None,
                       sleep=time.sleep, out=None) -> int:
    """One protected round trip on the Binance SPOT TESTNET (fake money): risk engine, OPOCO entry,
    protection confirmed, short hold, close, nothing left open. Recorded toward the 5-clean-round-trips gate.
    Exit codes: 0 clean, 1 not clean (see the reason), 2 setup problem, 3 act now (coins may be unprotected)."""
    from bot.core.ids import new_intent_id
    from bot.core.money import D, pct
    from bot.exchange.binance import BinanceReadOnly, ExchangeError
    from bot.exchange.credentials import CredentialsError, load_credentials
    from bot.exchange.trading import BinanceTrader, OrderStatusUnknown
    from bot.execution import entry, roundtrip
    from bot.market.data import MarketDataService, MarketDataUnavailable
    from bot.risk.models import AccountState, Mode, TradeIntent

    out = out or sys.stdout
    cfg = load_config(args.config)
    journal_dir = Path(os.path.expanduser(args.journal_dir))
    load_creds = load_creds or (lambda: load_credentials(prefix="BINANCE_TESTNET"))
    out.write("Binance TESTNET round trip (fake money; testnet.binance.vision)\n")
    try:
        creds = load_creds()
    except CredentialsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    trader = (make_trader or BinanceTrader)(environment="testnet", credentials=creds)
    reader = (make_reader or BinanceReadOnly)(environment="testnet")
    wide = type(cfg).model_validate({**cfg.model_dump(), "universe": sorted({*cfg.universe, args.symbol})})
    data = MarketDataService(reader, wide)
    try:
        info = trader.account()
        if trader.open_orders(args.symbol):
            print(f"Error: there are open orders on {args.symbol} in the testnet account; cancel them first",
                  file=sys.stderr)
            return 2
        usdt = next((b.free for b in info.balances if b.asset == "USDT"), D(0))
        equity = min(usdt, D(args.equity))
        rules = data.rules(args.symbol)
        snap = data.snapshot(args.symbol)
    except (ExchangeError, MarketDataUnavailable) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    # A test account of `equity` USDT with nothing open: the risk engine sizes the trade on that.
    state = AccountState(mode=Mode.RUNNING, equity=equity, free_quote=equity, day_start_equity=equity,
                         week_start_equity=equity, daily_pnl=D(0), weekly_pnl=D(0), drawdown_pct=D(0))
    entry_price = snap.best_ask * (1 + pct(D("0.10")))
    stop = entry_price * (1 - pct(D(args.stop_pct)))
    intent = TradeIntent(symbol=args.symbol, side="BUY", entry_price=entry_price, stop_price=stop,
                         take_profit_price=entry_price + 3 * (entry_price - stop), strategy="testnet-roundtrip",
                         horizon_hours=1, invalidation="testnet round trip: closed after the hold")
    out.write(f"  Account ........ sized as {equity:,.2f} USDT of testnet money; ask {snap.best_ask.normalize()}\n")
    try:
        outcome = entry.submit(intent, state, snap, rules, wide, trader, new_intent_id(), "majors", sleep=sleep)
    except OrderStatusUnknown as e:
        out.write(f"  Entry .......... OUTCOME UNKNOWN: {e}\n  Check the testnet account before running again.\n")
        return 3
    p = outcome.plan
    out.write(f"  Risk engine .... {'accepted' if outcome.decision.accepted else 'REJECTED'}: {outcome.decision.reason}\n")
    if p is not None:
        out.write(f"  Order list ..... BUY {p.qty.normalize()} at {p.entry_price.normalize()} fill-or-kill; "
                  f"stop {p.stop_type} {p.stop_price.normalize()}"
                  f"{f' (limit {p.stop_limit_price.normalize()})' if p.stop_limit_price else ''}; "
                  f"take-profit {p.take_profit_price.normalize()}\n")
    out.write(f"  Entry .......... {outcome.status}: {outcome.reason}\n")
    for n in outcome.notes:
        out.write(f"  Note ........... {n}\n")
    if outcome.status == entry.UNPROTECTED:
        out.write("  ACT NOW: the coins may be unprotected on the testnet; sell them there by hand.\n")
        rt = roundtrip.RoundTrip(outcome, problems=[outcome.reason])
        roundtrip.record(journal_dir, rt, time.time())
        return 3
    if outcome.status == entry.PROTECTED:
        out.write(f"  Protection ..... stop {outcome.stop.status} {outcome.stop.orig_qty.normalize()}, take-profit "
                  f"{outcome.take_profit.status} {outcome.take_profit.orig_qty.normalize()}; holding {args.hold:g} s\n")
        sleep(args.hold)
    rt = roundtrip.close(outcome, trader, rules) if outcome.status == entry.PROTECTED else \
        roundtrip.RoundTrip(outcome, problems=[f"entry was {outcome.status}"])
    if rt.close is not None:
        out.write(f"  Close .......... SELL {rt.close.executed_qty.normalize()} at market: {rt.close.status}\n")
    for problem in rt.problems:
        out.write(f"  Problem ........ {problem}\n")
    clean = roundtrip.record(journal_dir, rt, time.time())
    out.write(f"Result: {'CLEAN round trip' if rt.clean else 'NOT clean'}. Clean round trips so far: "
              f"{clean} of {roundtrip.GATE} (journal: {journal_dir / roundtrip.JOURNAL})\n")
    return 0 if rt.clean else 1


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
    from bot.paper.runner import STATE_FILE

    cfg = load_config(args.config)
    state_dir = _paper_dir(args)
    if not (state_dir / STATE_FILE).exists():
        print(f"No paper account yet in {state_dir}. Start one with: bot paper run")
        return 1
    print(f"Paper account in {state_dir} (simulated money only)")
    _print_account(state_dir, cfg, gate=True)
    return 0


def _print_account(state_dir: Path, cfg, gate: bool):
    """Runner, mode, equity, positions and trades of a saved simulated account; returns the simulator."""
    import json

    from bot.core.money import fmt
    from bot.paper import store
    from bot.paper.runner import STATE_FILE

    data = json.loads((state_dir / STATE_FILE).read_text())
    sim = store.from_dict(data, cfg, market=None, clock=lambda: data["saved_at"])
    D = Decimal
    equity = sim.equity()
    start = sim.starting_quote
    trades = sim.trades
    wins = sum(1 for t in trades if t.pnl > 0)
    fees = sum((f.fee for f in sim.fills), D(0))
    days = (data["saved_at"] - sim.created_at) / 86400
    saved = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(data["saved_at"]))
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
    if gate:
        print(f"Paper review gate: {days:.1f} of {PAPER_GATE_WEEKS * 7} days, {len(trades)} of {PAPER_GATE_TRADES} "
              f"trades, {wins} of {PAPER_GATE_PROFITABLE} profitable (rule violations are reviewed separately)")
    return sim


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
        print(f"Request '{kind}' queued for the runner in {state_dir}.")
        if kind == "trade":
            print("It still goes through the risk engine; see the result in: bot paper status, or journal.jsonl")
        if not _runner_active(state_dir):
            print("Note: the runner is not running; the request waits until it starts.")
        return 0
    return handler


DEFAULT_DATA_DIR = "~/backtest-data"
DEFAULT_REPORTS_DIR = "~/backtests"
DEFAULT_FORWARD_DIR = "~/forward-h2b"
FORWARD_LABEL = "FORWARD TEST of H2b (simulated money only; does not count toward the Stage 6 paper review)"


def _forward_cfg(base, symbols):
    """The config file with the forward test's four coins as its universe (as the H2b backtest used)."""
    return type(base).model_validate({**base.model_dump(), "universe": list(symbols)})


def _forward_start(args: argparse.Namespace, out=None) -> int:
    """Sultan only, once: fixes H2b's settings, coins and fingerprints before the first trade."""
    from bot import forward
    from bot.backtest import history, report, universe

    out = out or sys.stdout
    state_dir = Path(os.path.expanduser(args.state_dir))
    data_dir = Path(os.path.expanduser(args.data_dir))
    base = load_config(args.config)
    try:
        if args.confirm != forward.CONFIRM_PHRASE:
            raise forward.ForwardError(f'type --confirm "{forward.CONFIRM_PHRASE}" exactly to start the forward test')
        u = universe.load_universe(data_dir)
        forward.check_coins(list(u["symbols"]), u["groups"])
        cfg = _forward_cfg(base, u["symbols"])
        research = {s: history.load(data_dir, s, "4h") for s in u["symbols"]}
        rules = {s: history.load_rules(data_dir, s) for s in u["symbols"]}
        need, free = universe.memory_needed_mb(len(research)), universe.available_memory_mb()
        if free is not None and free < need + 30:
            raise forward.ForwardError(f"choosing the settings needs about {need} MB of memory plus a margin and "
                                       f"only {free} MB is free; stop the paper service for a few minutes "
                                       f"(systemctl stop bot-paper) and try again")

        def say(msg):
            out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
            out.flush()

        commit = report.provenance(base, {}, report.CostModel())["git_commit"]
        ft = forward.start(state_dir, research, rules, cfg, base, args.operator, args.confirm, time.time(),
                           u["groups"], commit, progress=say)
    except (forward.ForwardError, history.DataError) as e:
        print(f"Error: {e}\nThe forward test was not started.", file=sys.stderr)
        return 2
    out.write(f"\n{FORWARD_LABEL}\nStarted by {ft.operator} on {forward.when(ft.started_at)}; recorded in "
              f"{state_dir / forward.FORWARD_FILE}.\n"
              f"Settings (fixed for the whole test): {', '.join(f'{k}={v}' for k, v in ft.params.items())}\n"
              f"Coins: {'; '.join(f'{s} ({g})' for s, g in ft.corr_groups.items())}\n"
              f"Next, as root: bash /home/bot/crypto-bot/scripts/install_forward_service.sh\n")
    return 0


def _forward_run(args: argparse.Namespace, make_exchange=None, out=None) -> int:
    """The forward-test runner (the bot-forward service): the paper runner with H2b and no manual trades."""
    from bot import forward
    from bot.exchange.binance import BinanceReadOnly
    from bot.paper.runner import PaperRunner, RunnerLock, RunnerLocked
    from bot.paper.store import StateError

    base = load_config(args.config)
    if base.environment.value not in ("paper", "sim") or base.live_trading:
        print(f"Error: the forward test needs environment paper or sim (config says {base.environment.value})",
              file=sys.stderr)
        return 2
    state_dir = Path(os.path.expanduser(args.state_dir))
    try:
        ft = forward.load(state_dir)
        forward.check_unchanged(ft, base)
        strategy = forward.make_strategy(ft)
        market = (make_exchange or BinanceReadOnly)(environment=_binance_host_for(base.environment.value))
        with RunnerLock(state_dir):
            runner = PaperRunner.open(state_dir, _forward_cfg(base, ft.symbols), market, out=out or sys.stdout,
                                      strategies=[strategy], manual_trades=False,
                                      sim_kw={"corr_groups": dict(ft.corr_groups)})

            def stop(signum, frame):
                runner.stop_requested = True

            old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
            try:
                runner.run(interval_s=args.interval, max_ticks=args.ticks)
            finally:
                for s, h in old.items():
                    signal.signal(s, h)
    except (forward.ForwardError, RunnerLocked, StateError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


def _forward_report(args: argparse.Namespace, make_exchange=None, out=None) -> int:
    """The end-of-test report: judged once, after the test has ended, with the runner stopped."""
    import json

    from bot import forward, forward_report
    from bot.backtest import history, report, universe
    from bot.exchange.binance import BinanceReadOnly
    from bot.paper import store
    from bot.paper.runner import CLOCK_FILE, STATE_FILE

    out = out or sys.stdout
    state_dir = Path(os.path.expanduser(args.state_dir))
    data_dir = Path(os.path.expanduser(args.data_dir))
    base = load_config(args.config)
    try:
        ft = forward.load(state_dir)
        saved = forward_report.read_verdict(state_dir)
        if saved is not None:
            out.write(f"Already judged on {saved['judged_at']} by {saved['operator']}: {saved['verdict']}\n"
                      f"Report: {saved['report']}\n")
            return 0
        if not args.operator.strip():
            raise forward.ForwardError("give your name with --operator")
        if not (state_dir / STATE_FILE).exists():
            raise forward.ForwardError("the forward test has no account yet; nothing to judge")
        if _runner_active(state_dir):
            raise forward.ForwardError("stop the forward-test service first, so the account no longer changes "
                                       "(as root: systemctl stop bot-forward)")
        cfg = _forward_cfg(base, ft.symbols)
        data = json.loads((state_dir / STATE_FILE).read_text())
        sim = store.from_dict(data, cfg, market=None, clock=lambda: data["saved_at"])
        clock = state_dir / CLOCK_FILE
        last = json.loads(clock.read_text()).get(ft.strategy) if clock.exists() else None
        now = time.time()
        prog = forward.progress(ft, forward.read_journal(state_dir), last, len(sim.trades), now)
        if prog.state == "running":
            raise forward.ForwardError(f"the forward test has not ended: it can be judged from "
                                       f"{forward.when(prog.earliest_end)} once {forward.MIN_TRADES} trades have "
                                       f"closed ({len(sim.trades)} so far), or on {forward.when(prog.latest_end)}")
        step = 4 * 3_600_000
        end_ms = int(min(now, prog.latest_end) * 1000) // step * step
        need, free = universe.memory_needed_mb(len(ft.symbols)), universe.available_memory_mb()
        if free is not None and free < need + 30:
            raise forward.ForwardError(f"the report needs about {need} MB of memory plus a margin and only {free} MB "
                                       f"is free; stop the paper service for a while (systemctl stop bot-paper)")
        venue = (make_exchange or BinanceReadOnly)(environment="live")
        warm_from = int(ft.started_at * 1000) - (forward.make_strategy(ft).history_needed() + 60) * step
        for s in ft.symbols:                      # extends the cache (it starts in 2019, before warm_from)
            history.fetch(venue, data_dir, s, "4h", start_ms=warm_from, end_ms=end_ms)
        candles = {s: [c for c in history.load_with_forward(data_dir, s, "4h") if warm_from <= c.open_time
                       and c.close_time < end_ms] for s in ft.symbols}
        short = [s for s, cs in candles.items() if not cs or cs[-1].close_time + 1 < end_ms or cs[0].open_time > warm_from]
        if short:
            raise history.DataError(f"price history does not cover the forward test for {', '.join(short)}; "
                                    f"nothing is judged on incomplete prices")
        rules = {s: history.load_rules(data_dir, s) for s in ft.symbols}

        def say(msg):
            out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
            out.flush()

        ev = forward_report.evaluate(ft, sim.trades, sim.fills, prog, candles, rules, cfg, end_ms,
                                     random_runs=args.random_runs, progress=say)
    except (forward.ForwardError, forward_report.ReportError, history.DataError) as e:
        print(f"Error: {e}\nNothing was judged.", file=sys.stderr)
        return 2
    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    text = forward_report.render(ev, ft, args.operator, generated)
    folder = report.write_files(Path(os.path.expanduser(args.reports_dir)), "forward-H2b", text,
                                {"forward_test": ft.__dict__, **forward_report.summary(ev)})
    try:
        forward_report.save_verdict(state_dir, {"judged_at": generated, "operator": args.operator,
                                                "verdict": forward_report.verdict(ev), "passes": ev.passes,
                                                "report": str(folder)})
    except forward_report.ReportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    out.write("\n" + text + f"\nReport saved in {folder}; the verdict is recorded in "
              f"{state_dir / forward_report.VERDICT_FILE} and is final.\n")
    return 0


def _forward_status(args: argparse.Namespace) -> int:
    import json

    from bot import forward
    from bot.paper.runner import CLOCK_FILE, STATE_FILE

    state_dir = Path(os.path.expanduser(args.state_dir))
    try:
        ft = forward.load(state_dir)
    except forward.ForwardError as e:
        print(str(e))
        return 1
    base = load_config(args.config)
    print(FORWARD_LABEL)
    print(f"Folder {state_dir}; started by {ft.operator} on {forward.when(ft.started_at)}; settings "
          f"{', '.join(f'{k}={v}' for k, v in ft.params.items())}; coins "
          f"{'; '.join(f'{s} ({g})' for s, g in ft.corr_groups.items())}")
    try:
        forward.check_unchanged(ft, base)
    except forward.ForwardError as e:
        print(f"STOPPED: {e}")
    if not (state_dir / STATE_FILE).exists():
        print("The runner has not started yet: bash /home/bot/crypto-bot/scripts/install_forward_service.sh")
        return 0
    sim = _print_account(state_dir, _forward_cfg(base, ft.symbols), gate=False)
    clock = state_dir / CLOCK_FILE
    last = json.loads(clock.read_text()).get(ft.strategy) if clock.exists() else None
    p = forward.progress(ft, forward.read_journal(state_dir), last, len(sim.trades), time.time())
    print(f"Day {p.days:.0f} of at least {forward.MIN_DAYS} (at most {forward.MAX_DAYS}); closed trades "
          f"{p.trades} of at least {forward.MIN_TRADES}")
    print(f"Decisions missed: {p.missed_hours} hours ({p.outage_share * 100:.1f}% of the time"
          + ("; above 10%, so the end dates move by the time missed)" if p.outage_share > forward.OUTAGE_SHARE
             else "; up to 10% does not move the end dates)"))
    print(f"Can be judged from {forward.when(p.earliest_end)} once {forward.MIN_TRADES} trades have closed; "
          f"at the latest on {forward.when(p.latest_end)}")
    print({"running": "Status: running. Pass or fail is judged once, at the end, by the end-of-test report.",
           "ready to judge": "Status: READY TO JUDGE: stop bot-forward, then run: forward report --operator NAME",
           "ended": "Status: ENDED (24-month limit): stop bot-forward, then run: forward report --operator NAME",
           "failed": f"Status: FAILED: the 6% drawdown breaker tripped on "
                     f"{forward.when(p.failed_at or 0)}. H2b fails the forward test (plan section 3)."}[p.state])
    return 0


def _backtest_fetch(args: argparse.Namespace, make_exchange=None) -> int:
    """Download price history through the read-only adapter (public data, no API key)."""
    from bot.backtest import history
    from bot.exchange.binance import BinanceReadOnly

    cfg = load_config(args.config)
    venue = (make_exchange or BinanceReadOnly)(environment="live")
    data_dir = Path(os.path.expanduser(args.data_dir))
    symbols = args.symbols or cfg.universe
    if getattr(args, "screened", False):
        from bot.backtest import universe
        try:
            symbols = universe.load_universe(data_dir)["symbols"]
        except history.DataError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
    ok = True
    for symbol in symbols:
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


def _backtest_screen(args: argparse.Namespace, make_exchange=None, out=None) -> int:
    """Screen Binance USDT pairs for the wider H2 re-test (rules fixed in bot.backtest.universe)."""
    from bot.backtest import universe
    from bot.exchange.binance import BinanceReadOnly

    cfg = load_config(args.config)
    out = out or sys.stdout
    venue = (make_exchange or BinanceReadOnly)(environment="live")
    data_dir = Path(os.path.expanduser(args.data_dir))

    def say(msg):
        out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
        out.flush()

    try:
        res = universe.screen(venue, data_dir, cfg, progress=say)
    except Exception as e:
        print(f"Error: screen failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    text = universe.render(res)
    (data_dir / "screen" / "screen.md").write_text(text)
    out.write("\n" + text + f"\nSaved in {data_dir / 'screen'}. Next: bot backtest fetch --screened\n")
    return 0


def _research_setup(args: argparse.Namespace, cfg):
    """(config, symbols, correlation groups, description) for a backtest; --screened uses the screen result."""
    from bot.backtest import universe

    if not getattr(args, "screened", False):
        return cfg, list(cfg.universe), None, None
    u = universe.load_universe(Path(os.path.expanduser(args.data_dir)))
    wide = type(cfg).model_validate({**cfg.model_dump(), "universe": u["symbols"]})
    desc = "; ".join(f"{s} ({u['groups'].get(s, 'majors')})" for s in u["symbols"])
    return wide, list(u["symbols"]), u["groups"], desc


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


STRATEGIES = ("H1", "H2", "H3", "H2b")
HOLDOUT_STRATEGIES = ("H1", "H2", "H3")      # H2b was designed after the holdout was seen (docs/h2b-plan.md)


def _make_strategy(name: str, symbols: list[str]):
    from bot.strategies.h1_trend import TrendBreakout
    from bot.strategies.h2_momentum import TimeSeriesMomentum
    from bot.strategies.h2b_blend import BlendedMomentum
    from bot.strategies.h3_mean_reversion import RangeMeanReversion

    return {"H1": TrendBreakout, "H2": TimeSeriesMomentum, "H3": RangeMeanReversion,
            "H2b": BlendedMomentum}[name](symbols)


def _backtest_run(args: argparse.Namespace, out=None) -> int:
    """Walk-forward and robustness tests of H1-H3 and H2b on the research period. The holdout stays locked."""
    from bot.backtest import history, report, selection

    data_dir = Path(os.path.expanduser(args.data_dir))
    out = out or sys.stdout
    try:
        cfg, symbols, corr_groups, desc = _research_setup(args, load_config(args.config))
        candles = {s: history.load(data_dir, s, args.interval) for s in symbols}
        rules = {s: history.load_rules(data_dir, s) for s in symbols}
    except history.DataError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    def say(msg):
        out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
        out.flush()

    from bot.backtest.universe import available_memory_mb, memory_needed_mb
    need, free = memory_needed_mb(len(symbols)), available_memory_mb()
    if free is not None and free < need + 30:
        print(f"Error: this run needs about {need} MB of memory plus a margin, and only {free} MB is free. "
              f"Stop other programs or add swap, then try again.", file=sys.stderr)
        return 2
    cost = report.CostModel()
    meta = report.provenance(cfg, candles, cost)
    if desc:
        meta["universe"] = desc
    verdicts = []
    for name in args.strategy:
        strategy = _make_strategy(name, symbols)
        say(f"{name}: starting ({3 ** len(strategy.params())} setting combinations x 8 windows, "
            f"then robustness checks)")
        ev = selection.evaluate(strategy, candles, rules, cfg, cost,
                                random_runs=args.random_runs, progress=say, corr_groups=corr_groups)
        text = selection.render(ev, meta, cfg)
        folder = report.write_files(Path(os.path.expanduser(args.reports_dir)), f"strategy-{name}", text,
                                    {"meta": meta, **selection.summary(ev)})
        out.write("\n" + text + f"\nReport saved in {folder}\n\n")
        verdicts.append((name, ev.passes_walk_forward, folder))
    out.write("Summary (walk-forward part of the selection rule; the holdout is spent):\n")
    for name, ok, folder in verdicts:
        verdict = "passes walk-forward; next a forward test, if Sultan approves" if ok else "does not qualify"
        out.write(f"  {name}: {verdict}  ({folder})\n")
    return 0


def _backtest_holdout(args: argparse.Namespace, out=None) -> int:
    """The one-time holdout test. Every check that could stop it runs BEFORE the holdout is opened,
    so a mistake (wrong phrase, missing data, no passing walk-forward) never uses it up."""
    from bot.backtest import history, holdout, report
    from bot.backtest.periods import HOLDOUT_END_MS, HoldoutLocked
    from bot.backtest.universe import available_memory_mb, memory_needed_mb

    out = out or sys.stdout
    data_dir = Path(os.path.expanduser(args.data_dir))
    reports_dir = Path(os.path.expanduser(args.reports_dir))
    try:
        if args.strategy not in HOLDOUT_STRATEGIES:
            raise HoldoutLocked(f"{args.strategy} never runs on the holdout (docs/h2b-plan.md)")
        if args.confirm != holdout.CONFIRM_PHRASE:
            raise HoldoutLocked(f'type --confirm "{holdout.CONFIRM_PHRASE}" exactly to open the holdout')
        if not args.operator.strip():
            raise HoldoutLocked("give your name with --operator")
        cfg, symbols, corr_groups, desc = _research_setup(args, load_config(args.config))
        missing = [s for s in symbols if not history.covers(data_dir, s, args.interval, HOLDOUT_END_MS)]
        if missing:
            raise history.DataError(f"history does not reach 2026-10-01 for {', '.join(missing)}; "
                                    f"run: bot backtest fetch{' --screened' if args.screened else ''}")
        research = {s: history.load(data_dir, s, args.interval) for s in symbols}
        rules = {s: history.load_rules(data_dir, s) for s in symbols}
        meta = report.provenance(cfg, research, report.CostModel())
        if desc:
            meta["universe"] = desc
        wf_folder, wf = holdout.find_walk_forward(reports_dir, args.strategy, meta["config_sha256"], desc)
        need, free = memory_needed_mb(len(symbols)), available_memory_mb()
        if free is not None and free < need + 30:
            raise HoldoutLocked(f"this run needs about {need} MB of memory plus a margin and only {free} MB is "
                                f"free; the holdout was NOT opened")
    except (HoldoutLocked, history.DataError) as e:
        print(f"Error: {e}\nThe holdout was not opened.", file=sys.stderr)
        return 2

    ticket = holdout.open_holdout(data_dir, args.strategy, args.operator, args.confirm)
    out.write(f"Holdout opened by {args.operator} at {ticket.opened_at} and recorded in "
              f"{data_dir / holdout.LOG_FILE}.\n")
    if ticket.void:
        out.write(f"WARNING: the holdout was already used ({', '.join(ticket.previous_runs)}). "
                  f"This run is VOID and cannot qualify anything.\n")
    meta["holdout"] = f"opened by {args.operator} at {ticket.opened_at}"

    def say(msg):
        out.write(f"  {time.strftime('%H:%M:%S', time.gmtime())} {msg}\n")
        out.flush()

    through = {s: history.load_through_holdout(data_dir, s, args.interval, ticket) for s in symbols}
    ev = holdout.evaluate(_make_strategy(args.strategy, symbols), research, through, rules, cfg, wf_folder, wf,
                          ticket, corr_groups=corr_groups, progress=say)
    text = holdout.render(ev, meta, args.operator)
    folder = report.write_files(reports_dir, f"holdout-{args.strategy}", text,
                                {"meta": meta, **holdout.summary(ev, args.operator)})
    holdout.finish(data_dir, args.strategy, ticket, holdout.verdict(ev), str(folder))
    out.write("\n" + text + f"\nReport saved in {folder}\n")
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
    ct = sub.add_parser("check-trading", help="testnet trading key check and a test order (nothing is placed)")
    ct.add_argument("--symbol", default="BTCUSDT")
    tr = sub.add_parser("testnet-roundtrip", help="one protected round trip on the Binance testnet (fake money)")
    tr.add_argument("--symbol", default="BTCUSDT")
    tr.add_argument("--stop-pct", default="1.5", help="stop distance below the entry, in percent")
    tr.add_argument("--equity", default="1000", help="testnet USDT the risk engine sizes the trade on")
    tr.add_argument("--hold", type=float, default=10.0, help="seconds to hold before closing")
    tr.add_argument("--journal-dir", default=DEFAULT_TESTNET_DIR)
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
    fw = sub.add_parser("forward", help="the H2b forward test: live prices, simulated money, its own account")
    fsub = fw.add_subparsers(dest="forward_cmd", required=True)
    fs = fsub.add_parser("start", help="Sultan only, once: fix H2b's settings and coins, then start the clock")
    fs.add_argument("--operator", required=True)
    fs.add_argument("--confirm", required=True, help="the exact confirmation phrase")
    fs.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    fr = fsub.add_parser("run", help="run the forward test until stopped (used by the bot-forward service)")
    fr.add_argument("--interval", type=float, default=5.0, help="seconds between price checks")
    fr.add_argument("--ticks", type=int, default=None, help=argparse.SUPPRESS)
    fsub.add_parser("status", help="days, trades, missed decisions and the end dates")
    fp = fsub.add_parser("report", help="Sultan, once, after the test has ended: the 6 checks and the verdict")
    fp.add_argument("--operator", required=True)
    fp.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    fp.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    fp.add_argument("--random-runs", type=int, default=50, help="random-entry runs with the same exits")
    for name, help_ in (("resume", "allow new entries (refused while a breaker is active)"),
                        ("block", "block new entries; exits keep working")):
        q = fsub.add_parser(name, help=help_)
        q.add_argument("--operator", required=True)
        q.add_argument("--reason", required=True, help="10+ characters")
    for q in fsub.choices.values():
        q.add_argument("--state-dir", default=DEFAULT_FORWARD_DIR, help="where the forward-test account is kept")
    bp = sub.add_parser("backtest", help="price history and backtests (research period only; holdout locked)")
    bsub = bp.add_subparsers(dest="backtest_cmd", required=True)
    bsub.add_parser("screen", help="screen Binance USDT pairs for the wider H2 re-test (fixed rules)")
    bf = bsub.add_parser("fetch", help="download price history through the read-only adapter")
    bf.add_argument("--symbols", nargs="*", default=None, help="default: the config universe")
    bf.add_argument("--screened", action="store_true", help="the coins picked by bot backtest screen")
    bb = bsub.add_parser("baselines", help="cash, buy-and-hold, 50/50 and random entries on the research period")
    bb.add_argument("--seeds", type=int, default=200, help="number of random-entry runs")
    bb.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    br = bsub.add_parser("run", help="walk-forward and robustness tests of strategies H1-H3, H2b (research period)")
    br.add_argument("--strategy", nargs="+", choices=STRATEGIES, default=list(STRATEGIES))
    br.add_argument("--random-runs", type=int, default=50, help="random-entry runs with the same exits")
    br.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    br.add_argument("--screened", action="store_true",
                    help="use the coins and correlation groups picked by bot backtest screen")
    bh = bsub.add_parser("holdout", help="the one-time holdout test (Sultan only; recorded; a second run is VOID)")
    bh.add_argument("--strategy", required=True, choices=HOLDOUT_STRATEGIES)
    bh.add_argument("--operator", required=True)
    bh.add_argument("--confirm", required=True, help="the exact confirmation phrase")
    bh.add_argument("--screened", action="store_true", help="the coins and groups picked by bot backtest screen")
    bh.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    for q in bsub.choices.values():
        q.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
        q.add_argument("--interval", default="4h", choices=["4h"])
    args = p.parse_args(argv)
    try:
        if args.cmd == "backtest":
            return {"screen": _backtest_screen, "fetch": _backtest_fetch, "baselines": _backtest_baselines,
                    "run": _backtest_run, "holdout": _backtest_holdout}[args.backtest_cmd](args)
        if args.cmd == "forward":
            return {"start": _forward_start, "run": _forward_run, "status": _forward_status, "report": _forward_report,
                    "resume": _paper_request("resume"), "block": _paper_request("block")}[args.forward_cmd](args)
        if args.cmd == "paper":
            return {"run": _paper_run, "status": _paper_status, "resume": _paper_request("resume"),
                    "block": _paper_request("block"), "trade": _paper_request("trade")}[args.paper_cmd](args)
        return {"check-config": _check_config, "try-trade": _try_trade,
                "check-binance": _check_binance, "check-trading": _check_trading,
                "testnet-roundtrip": _testnet_roundtrip}[args.cmd](args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
