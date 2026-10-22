"""Command-line entry point.

    bot check-config [--config config/default.yaml]
    bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 [--symbol BTCUSDT]
    bot exchange-check [--env testnet] [--symbols BTCUSDT,ETHUSDT]

`try-trade` runs a proposed trade through the real risk engine with example
market data and prints the decision in plain language. It sends nothing to Binance.

`exchange-check` connects to Binance read-only and prints clock skew, symbol
rules and best bid/ask; with BINANCE_API_KEY set it also prints balances and
commission rates. It cannot place orders.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from bot.core.config import Environment, load_config
from bot.exchange.binance import BinanceReadOnly, ExchangeError, load_credentials
from bot.exchange.filters import parse_symbol
from bot.exchange.market import ExchangeDataError
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


REPO_ROOT = Path(__file__).resolve().parents[2]
_connect = BinanceReadOnly.connect   # replaced in tests


def _exchange_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    env = Environment(args.env) if args.env else cfg.environment
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(cfg.universe)
    problems: list[str] = []
    try:
        creds = load_credentials(repo_root=REPO_ROOT)
        print(f"Connecting read-only to Binance ({env.value}); API key {'loaded' if creds else 'not set, public data only'}")
        ex = _connect(env, creds)
        skew = ex.clock_skew_ms()
        ok = skew <= cfg.risk.max_clock_skew_ms
        print(f"Clock skew: {skew} ms ({'OK' if ok else 'TOO HIGH'}, limit {cfg.risk.max_clock_skew_ms} ms)")
        if not ok:
            problems.append("clock skew above limit; sync the system clock")
        rules = ex.symbol_rules(symbols)
        books = ex.book_tickers(symbols)
        for sym in symbols:
            r, b = rules[sym], books[sym]
            print(f"{sym}: status {r.status}, tick {r.tick_size}, step {r.step_size}, min order {r.min_notional} USDT, "
                  f"OCO {r.oco_allowed}, OPO {r.opo_allowed}, MAX_ASSET {r.max_asset_qty or '-'}")
            print(f"  bid {b.bid} / ask {b.ask}, spread {b.spread_pct:.4f}%")
            if not r.is_trading:
                problems.append(f"{sym} is not trading ({r.status})")
        if creds:
            acct = ex.account()
            print(f"Account: type {acct.account_type}, permissions {', '.join(acct.permissions) or '-'}")
            if acct.can_withdraw:
                print("  Note: the account reports canWithdraw=true. Make sure the API key itself has "
                      "withdrawals disabled in Binance API management (this bot never withdraws).")
            for bal in acct.balances:
                print(f"  {bal.asset}: free {bal.free}, locked {bal.locked}")
            for sym in symbols:
                c = ex.commission(sym)
                print(f"  {sym} fees: maker {c.maker}, taker {c.taker} (BNB discount ignored)")
    except (ExchangeError, ExchangeDataError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        print("New entries would be blocked (fail closed).", file=sys.stderr)
        return 3
    for p in problems:
        print(f"PROBLEM: {p}")
    print("Read-only check finished; no orders were sent." if not problems else "Read-only check found problems.")
    return 1 if problems else 0


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
    e = sub.add_parser("exchange-check")
    e.add_argument("--env", choices=[v.value for v in Environment if v is not Environment.SIM],
                   help="defaults to the config's environment")
    e.add_argument("--symbols", help="comma-separated; defaults to the config universe")
    args = p.parse_args(argv)
    try:
        return {"check-config": _check_config, "try-trade": _try_trade,
                "exchange-check": _exchange_check}[args.cmd](args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
