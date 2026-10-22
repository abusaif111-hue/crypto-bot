"""Command-line entry point.

    bot check-config [--config config/default.yaml]
    bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 [--symbol BTCUSDT]

`try-trade` runs a proposed trade through the real risk engine with example
market data and prints the decision in plain language. It sends nothing to Binance.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal

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
    args = p.parse_args(argv)
    try:
        return {"check-config": _check_config, "try-trade": _try_trade,
                "check-binance": _check_binance}[args.cmd](args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
