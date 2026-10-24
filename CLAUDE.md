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
   **H2 failed the holdout (23 Oct 2026) and is rejected; the holdout year is spent** and no
   strategy may be judged on it again. Next is H2b, defined in `docs/h2b-plan.md` (approved by Sultan,
   frozen): never edit that plan or tune H2b to pass; any change is a new strategy with a new forward
   test. H2b never runs on the holdout. Its real test is a forward test on new prices (simulated money).
   H2b is `bot.strategies.h2b_blend` (fixed values pinned by `tests/unit/test_h2b.py`). The holdout
   lock is stricter: any earlier opening, by any strategy, makes every later run VOID. H2b passed the
   walk-forward check (23 Oct 2026; 104 trades, +1.90 USDT per trade with doubled costs). The paper runner
   asks strategies once per finished candle and applies their exits to their own positions only
   (`strategy-clock.json`). The forward test is `bot.forward` + `bot forward start|run|status|resume|block`
   (service `bot-forward`, account `~/forward-h2b`): only Sultan starts it; never edit `forward.json`, never
   change `h2b_blend.py` or `strategies/base.py` while it runs (the runner refuses: a change restarts the test).
   The end-of-test report is `bot.forward_report` + `bot forward report` (Sultan, once, after the end; verdict
   in `verdict.json`, never overwritten). Its replays use `engine.run(forward_from=...)` and
   `history.load_with_forward`, the only place candles from the spent holdout year may be read, as indicator
   history before the forward start; nothing there is traded or measured (enforced by tests). Do not change
   the report's checks after the forward test has started.
   H4 (`docs/h4-plan.md`, hourly pullbacks) was approved by Sultan on 24 Oct 2026 and is frozen: never edit
   that plan or tune H4 to pass; build it as written, never touch H2b's forward test for it. If 1h data for
   four coins does not fit the server's memory, H4 uses BTC and ETH only (his decision 4a).
5. Execution: idempotent orders, OPOCO entry with FOK (verify on testnet) or IOC + OCO fallback, protection verification, reconciliation
   (5a done: `bot.exchange.trading.BinanceTrader`, testnet only, 7 allow-listed endpoints, `bot check-trading`;
   only `bot.execution` and the CLI may import it). Never re-send an order whose outcome is unknown: Binance
   accepts a repeated client order ID once the first order filled. Look it up; if still unknown, block entries.
   The testnet key is `BINANCE_TESTNET_*`, separate from the live read-only key.
   5b done: `bot.execution.entry.submit()` is the only way to open an exchange position; it calls `evaluate()`
   itself, sends one OPOCO list (BUY LIMIT FOK, SELL LIMIT_MAKER take-profit, SELL STOP_LOSS or STOP_LOSS_LIMIT
   stop), confirms both protective orders after the fill, and on any doubt sells at market and blocks entries.
   `bot testnet-roundtrip` runs one protected trade on the testnet; clean ones count toward the gate
   (`~/testnet/roundtrips.jsonl`). Gate met 23 Oct 2026: 5 clean BTC round trips.
   5c done: `bot.execution.ledger` (a trade is written PENDING before its order is sent; OPEN, CLOSED,
   NOT_OPENED; the baseline is coins that are not the bot's, set only by an operator with nothing open)
   and `bot.execution.reconcile` (read-only: never places or cancels; anything that differs, an unknown
   outcome or an exchange error blocks new entries). `bot reconcile`; `testnet-roundtrip` reconciles
   before and after. Never loosen what reconciliation checks to make it pass.
   5d done: `bot.execution.runner.TestnetRunner` (service `bot-testnet`, state `~/testnet`): every round it
   reconciles (unprotected coins are sold at once; mismatches block until an operator resumes), marks the
   capped account (`bot.execution.account`: capital + changes since the start), runs time stops, then H2b
   once per finished candle with the forward test's frozen settings; entries are written PENDING first and
   go only through `entry.submit()`. Exits (`bot.execution.exit.close_trade`) never send a sale twice.
   `bot testnet start|run|status|resume|block`. Testnet only; the live switch is Sultan's.
   5e done: `bot.alerts` (service `bot-alerts`): reads the runners' journals and sends Telegram messages
   (🔴/🟡/🟢, repeats held back, short outages swallowed, silence and a daily check reported). It imports
   nothing from `bot` (it cannot trade); the token is typed hidden by `bot alerts setup`, kept in `.env`,
   and scrubbed from every error.
   5f done (live mode, OFF until Sultan switches it on): `BinanceTrader(environment="live")` reads and sends
   test orders, but its order endpoints need a `LiveAuthorization`, made only by `authorize_live()` from the
   config (environment live, live_trading true) AND `BOT_LIVE_CONFIRM`. Separate live trading key
   (`BINANCE_LIVE_TRADE_*`; `trading_key_problems` refuses one that can withdraw, transfer or use margin);
   `config/live.yaml` (not in git) may differ from default.yaml only in the switch; the live account
   (`~/live`, service `bot-live`) is capped at `LIVE_CAPITAL_CEILING_USDT` and sizes on the capital at most.
   `bot live check|baseline|reconcile|start|run|status|resume|block`. Never write live.yaml, the phrase or
   `live_trading: true` anywhere (a test checks the repo); never raise the ceiling without Sultan.
   **Sultan's decision (23 Oct 2026):** after the testnet gate (5 clean round trips), go live with a small,
   capped amount without waiting for the paper review; the H2b forward test keeps running. Sultan sets the
   cap and flips the live switch himself (1 USDT, his first figure, is below Binance's 5 USDT minimum order).
6. Loss-review loop, dashboard (FastAPI, not public), Telegram alerts
   Dashboard done: `bot.dashboard` (service `bot-dashboard`), Python's own HTTP server instead of FastAPI (no new
   dependency on the 512 MB server). Read-only: imports nothing from `bot`, never reads `.env`, GET only, binds
   127.0.0.1 only (`serve` refuses any other address); opened through an SSH tunnel. Telegram alerts: 5e.
   `bot signals` (`bot.signal_report`): read-only per-coin view of H2b's daily checks; the verdict is
   `BlendedMomentum.signal()` itself; it never changes `h2b_blend.py`, places orders or writes files.
7. News + LLM, advisory/veto only

Gates before live: testnet with at least 5 clean round trips; paper trading for
8+ weeks and 30+ closed trades with at least 5 profitable and zero rule violations.

## Current settings worth knowing

- Day/week boundary 00:00 UTC (04:00 Muscat); week starts Monday.
- Drawdown breaker 6% (ceiling 10%), cleared only by `complete_drawdown_review`.
- Stop-slippage allowance: BTC 0.5%, ETH 1.0% (ETH raised after the history review:
  24 days with moves beyond 10% in three years).
- BTC and ETH correlate at about 0.85: treat them as one correlation group.
