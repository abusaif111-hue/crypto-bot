# Crypto bot — instructions for Claude Code

Risk-first automated trading bot for **Binance Spot**, owned by Sultan. Capital
preservation comes before trade frequency or returns. The full design is the
"Binance AI Crypto Trading Bot — Stage 1 Design Package" (architecture, risk
rules R-01..R-14, threat model, stages); the 3-year BTC/ETH history review fed
into the settings. Rule IDs in code and tests refer to that package.

## Non-negotiable rules

1. **Every order goes through `bot.risk.engine.evaluate()`.** No code path may
   place, size or modify an entry order without an accepted `Decision`.
2. **Never weaken a risk limit.** Do not raise the values in `CEILINGS` or
   `FLOORS` (`src/bot/core/config.py`), remove a check from `evaluate()` or
   `_verify()`, or loosen a test to make it pass. If a limit seems wrong, stop
   and ask Sultan.
3. **Spot, long-only, USDT pairs.** No margin, futures, leverage, borrowing or shorting.
4. **No withdrawals or transfers, ever.** Never import a wallet SDK or call
   `/sapi/*/capital/withdraw` or `/asset/transfer`. `tests/unit/test_architecture.py` enforces this.
5. **Live trading stays off.** Never set `live_trading: true`, never write the
   `BOT_LIVE_CONFIRM` phrase into any file, and never advance a deployment stage.
   Only Sultan does that.
6. **Secrets.** Never ask for, print, log or commit API keys. Keys live in `.env`
   or a secrets file outside the repo. `.env.example` holds names only.
7. **The LLM/news modules are advisory.** `bot.news` and `bot.llm` may lower a
   score or veto; they must never import `bot.execution` or `bot.risk` internals.
8. **No invented results.** Never fabricate backtest, paper or live numbers.
   Label untested behaviour and unverified Binance features ("verify on testnet").
9. **A bot never rewrites its own rules after a loss.** Loss reviews propose
   changes; changes need a backtest, paper trading and Sultan's approval.

## Engineering conventions

- Python 3.11+. Money and quantities are `Decimal` (`bot.core.money.D`), never float.
- Quantities round **down** to the step size; size is never rounded up.
- Risk, portfolio and core code is pure: no network or file I/O (enforced by tests).
- Fail closed: missing data, failed reconciliation or an error blocks new entries.
- Binance access only through the adapter in `bot.exchange` (official
  `binance-sdk-spot`). Check current Binance docs before relying on an endpoint;
  REST listenKey user streams were retired in Feb 2026, use the WebSocket API
  `userDataStream.subscribe.signature`.
- `binance-sdk-spot` 13.2.0's generic `send_request`/`send_signed_request` crash;
  `bot.exchange.binance._SdkGetClient` calls the SDK's own `binance_common.utils.send_request`
  helper instead (same session, config and Ed25519 signer). The read-only adapter may only
  call endpoints in `READ_ENDPOINTS`; never add an order endpoint there.
- `bot.paper` and `bot.market` depend only on `bot.exchange.interface.SpotReadVenue`, never on
  the adapter itself. In the simulator, only `SimExchange.submit()` opens a position, and it
  calls `evaluate()` first; no method may take a ready-made `Decision` (enforced by tests).
- Paper trading (`bot paper run`, `bot.paper.runner`): only the runner changes the paper account;
  the CLI talks to it through request files. A restart restores the account with entries blocked
  and all breakers kept; an unreadable state file stops the runner and is never reset.
- Backtests: the holdout (2025-10-01 to 2026-10-01, `bot.backtest.periods`) is locked; never read,
  load or run on it, and never move the dates. Only Sultan runs the holdout command. Strategies
  (`bot.strategies`) and indicators (`bot.analysis`) are pure, see finished candles only, have at
  most 3 tunable settings, and only propose `TradeIntent`s; backtests replay them through
  `SimExchange`, so entries go through `evaluate()`. Results come only from real runs on the server.
- Every new rule or module ships with tests. Run `pytest` before finishing;
  all tests must pass. Keep the property test at 3,000 examples or more.
- One module per piece of work, small and reviewable.

## Commands

```bash
pip install -e ".[dev]"
pytest
bot check-config
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000
bot paper run --interval 5          # paper trading, simulated money; state in ~/paper
bot paper status
```

## Build order (from the design package)

1. ~~Core, risk engine, breakers, accounting~~ (done, module 1)
2. ~~Binance adapter, **read-only**: exchangeInfo, myFilters, commission, balances, klines, book ticker, key permissions~~ (done, module 2: `bot.exchange.binance`, `bot check-binance`)
3. ~~Market data service + simulated exchange for paper trading (same interface as the adapter)~~ (done, module 3: `bot.market.data`, `bot.paper.sim`)
4. Backtester (fees, slippage, no look-ahead, walk-forward, locked 12-month holdout) + strategies H1–H3 vs baselines
   (steps 1-2 done: data, replay, engine, metrics, baselines; strategies H1-H3, walk-forward, selection rule;
   step 3: holdout, run by Sultan). Backtests have two modes: as deployed (6% breaker halts) and edge
   measurement (breach recorded, peak reset; backtests only). Selection: every walk-forward test window
   must stay below the 6% breaker as deployed (Sultan's decision). First run: none qualified; H2 failed
   only the 100-trade check and is re-tested unchanged on more coins (`bot backtest screen`, rules fixed
   in `bot.backtest.universe` before any data was seen; R-08 groups by measured correlation). Do not
   change H2, the screen rules or the selection rule to make it pass; the wider universe is backtest-only.
   H2 passed walk-forward on BTC/ETH/XRP/BNB. Step 3 is `bot backtest holdout` (`bot.backtest.holdout`):
   only Sultan runs it; never run, simulate or preview it, never create a `HoldoutTicket` elsewhere, and
   never change its pass rules after a result.
5. Execution: idempotent orders, OPOCO entry with FOK (verify on testnet) or IOC + OCO fallback, protection verification, reconciliation
6. Loss-review loop, dashboard (FastAPI, not public), Telegram alerts
7. News + LLM, advisory/veto only

Gates before live: testnet with at least 5 clean round trips; paper trading for
8+ weeks and 30+ closed trades with at least 5 profitable and zero rule violations.

## Current settings worth knowing

- Day/week boundary 00:00 UTC (04:00 Muscat); week starts Monday.
- Drawdown breaker 6% (ceiling 10%), cleared only by `complete_drawdown_review`.
- Stop-slippage allowance: BTC 0.5%, ETH 1.0% (ETH raised after the history review:
  24 days with moves beyond 10% in three years).
- BTC and ETH correlate at about 0.85: treat them as one correlation group.
