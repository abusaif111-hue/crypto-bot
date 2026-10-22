# Crypto bot — modules 1–2: risk foundation and read-only Binance connection

This is the first working module of the Binance Spot trading bot described in the
Stage 1 design package. It contains the parts every later module depends on:

| Part | Folder | What it does |
| --- | --- | --- |
| Config with hard ceilings | `src/bot/core/config.py`, `config/default.yaml` | Loads settings; refuses any risk limit looser than its built-in ceiling; keeps live trading off unless explicitly confirmed |
| Exchange rules | `src/bot/exchange/filters.py` | Reads Binance symbol filters (tick size, step size, minimum order value, price bands, MAX_ASSET) and checks orders against them |
| Risk engine | `src/bot/risk/engine.py` | Sizes or rejects every proposed trade against rules R-01 to R-14 |
| Circuit breakers and modes | `src/bot/risk/modes.py` | Daily, weekly and drawdown breakers; RUNNING / ENTRIES_BLOCKED / PAUSED / EMERGENCY_FLATTEN |
| Accounting | `src/bot/portfolio/accounting.py` | Equity at best bid, daily and weekly P&L without deposits and withdrawals, drawdown index |
| Client order IDs | `src/bot/core/ids.py` | Deterministic IDs so a retried order can never be placed twice |
| Binance connection (read-only) | `src/bot/exchange/binance.py` | Module 2. Reads exchange rules, account MAX_ASSET filters, commission rates, balances, candles, best bid/ask and server time through the official `binance-sdk-spot` |
| Response parsers | `src/bot/exchange/market.py` | Turns Binance replies into exact `Decimal` data and rejects anything missing or impossible (empty or crossed book, bad candles, negative balances) |

**The bot still cannot trade.** The Binance connection can only call seven GET
endpoints; asking it for anything else raises an error, and a test fails if any
source file even names an order endpoint. Order sending comes in module 5,
behind the risk engine.

## The Binance connection (module 2)

What it reads, and from where:

| Data | Endpoint | Key needed |
| --- | --- | --- |
| Server time / clock skew (R-13) | `GET /api/v3/time` | no |
| Symbol rules (tick, step, minimum order, price bands) | `GET /api/v3/exchangeInfo` | no |
| Best bid / ask | `GET /api/v3/ticker/bookTicker` | no |
| Candles | `GET /api/v3/klines` | no |
| Balances | `GET /api/v3/account` | yes (read-only) |
| Commission rates | `GET /api/v3/account/commission` | yes |
| Account MAX_ASSET limits | `GET /api/v3/myFilters` | yes |

Safety behaviour:

- **Fail closed.** Network errors, server errors and rate limits become
  `ExchangeUnavailable`; a malformed reply becomes `ExchangeDataError`. Either
  means "no data": new entries are blocked. A missing price is never read as zero.
- **No look-ahead.** Candles that have not closed yet are dropped by default.
- **Rate limits.** After a 429/418 the connection pauses for the time Binance asks.
  When used weight passes 80% of the per-minute limit it pauses until the next minute.
- **Fees.** Taker fee = standard + special + tax commission. The BNB discount is
  ignored so sizing never assumes a fee lower than it might be.
- **Keys.** Read from `BINANCE_API_KEY` and `BINANCE_PRIVATE_KEY_PATH` (an Ed25519
  key file). A key file inside the repository is refused. The key is never printed.
- Spot USDT symbols only: a symbol that is not USDT-quoted or not Spot-enabled is refused.

**Not yet tested against Binance itself** (this was built without network access
to Binance). All behaviour above is tested with recorded-shape responses run
through the SDK's own response classes. Verify on testnet first:

```bash
bot exchange-check --env testnet                  # public data, no key needed
# then, with a testnet key in .env outside the repo:
bot exchange-check --env testnet
```

Create the key as **read-only**: Enable Reading only; Spot trading, withdrawals and
transfers off; restricted to the bot machine's IP. Binance does not let the bot see
a key's own permission settings without the wallet API, which this bot never uses,
so check them yourself in API Management.

## Run it

Needs Python 3.11 or newer.

```bash
cd crypto-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                              # 126 tests, about 20 seconds
bot check-config
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 --loss-today 150
bot exchange-check --env testnet    # read-only connection check, needs internet
```

`try-trade` runs a trade idea through the real risk engine using example market
data, and prints whether it is accepted, the size, the worst planned loss and why.

## How a trade is sized

1. Entry is assumed to fill slightly worse than planned (0.05%), and the stop
   worse again (0.5% for BTC, 1.0% for ETH and others), with the taker fee on both.
2. That gives the worst loss per coin bought.
3. The risk budget is the smallest of: 0.25% of equity per trade; what is left of
   today's 1.5% loss limit; what is left of the week's 3%; what is left of the
   1.5% total open-risk limit. Losses that could still happen at open
   positions' stops count against these budgets.
4. Quantity = budget ÷ worst loss per coin, then capped by the 50% invested limit,
   the 25% per-coin limit, free cash and order-book depth, and rounded **down**
   to Binance's step size.
5. If the result is below Binance's minimum order, the trade is skipped. Size is
   never rounded up and the risk limit is never raised to make a trade fit.

## What is tested

- Every rule has a worked example (`tests/unit/test_risk_engine.py`).
- A property-based test generates 3,000 random accounts, positions, prices and
  configs per run (about 700 of them produce accepted trades) and checks that no
  accepted trade breaks any limit. It was also run once at 20,000 cases.
- Circuit breakers: a daily breach holds until the next day; weekly and drawdown
  breaches never clear by themselves; resuming needs an operator name and a reason.
- Config: every value above a hard ceiling is refused; live mode needs both the
  config flag and the typed phrase in `BOT_LIVE_CONFIRM`.
- Architecture: no wallet, withdrawal or transfer code exists; the risk and
  accounting code makes no network calls and cannot import the news or AI modules;
  only `bot/exchange/binance.py` imports the Binance SDK; its allowlist holds only
  GET endpoints; no source file names an order, cancel or amend endpoint.
- Binance connection: parsing, fail-closed errors, rate-limit pauses, unclosed
  candles, key handling and the `exchange-check` command, using a fake client that
  returns the SDK's real response classes.

## Settings you may want to change

All in `config/default.yaml`. You can make limits stricter freely. Looser than the
built-in ceilings is refused at startup on purpose.

The drawdown breaker (6%) does not clear when prices recover: an operator must
complete a review first (`complete_drawdown_review`). If drawdown stays above the
limit, the only way to trade again is a deliberate config change, up to the 10% ceiling.

## Next modules

2. ~~Binance connection, read-only first (market data, balances, exchange rules)~~ (done)
3. Market data service and paper-trading simulator
4. Backtesting engine and the three candidate strategies
5. Order execution with protection checks and reconciliation
6. Dashboard and Telegram alerts
7. News and AI analysis (advisory only)
