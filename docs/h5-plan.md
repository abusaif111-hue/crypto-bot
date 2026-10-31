# H5 plan: daily breakout with orders placed in advance

Status: **APPROVED by Sultan on 31 Oct 2026 (decisions 1, 2 and 3 in section 7). Frozen.** Written
before any H5 result existed. The approving commit is the record of what was decided before any
result. Changing anything below after that (a setting, a coin, a date or a pass rule) makes it a new
strategy.

## 1. Why H5

Sultan wants buy orders that wait on Binance at a price and fill when the price gets there. That
only makes sense for a strategy whose buy rule *is* a price level. A breakout is such a rule ("buy if
the price rises above the highest price of the last 20 days"), so its order can be placed in advance
at exactly that level. H2b's rule is not a price level (it is decided at the daily close), so H2b
stays as it is, live and in its forward test, and is never changed for H5.

The research behind it: trend following is the best-supported crypto strategy family, and range
breakouts in particular have beaten buy-and-hold on Bitcoin's history, though often in studies that
ignored costs. H5 is tested with the full costs, doubled, before anything else.

H5 is different from what was already tested and rejected: H1 (4h breakout) and H1DT-BRK (1h
breakout) decided on short candles with tight stops, and costs ate them. H5 decides on **daily**
candles, with wide stops and few trades, where costs matter far less.

## 2. Costs

The simulator's own costs, unchanged (as in docs/h4-plan.md, section 2): 0.10% taker fee per fill,
0.01% half-spread, entry slippage 0.05%, stop slippage 0.5% (BTC) or 1.0% (ETH, XRP, BNB). A buy
that fills above the level because the price jumped past it pays that jump as well (section 3).
The pass rules use doubled costs. No cost is lowered for H5.

## 3. What H5 is

### Exact rules

Data: daily candles built from the 4h candles (days start at 00:00 UTC), finished days only.

Once a day, after the 00:00 UTC close, for each coin:

- **Level:** the highest high of the last `entry_days` finished days.
- **Order placed in advance, every day, one per coin (Sultan, 31 Oct 2026):** a buy that triggers
  when the price trades above the level, with a limit 0.5% above the level, valid until the next
  00:00 UTC. Then it is cancelled and a new one is placed at the new level. Every coin gets its order
  every day, unless the coin is already held or the risk engine refuses the order (the reason is
  recorded). A price that opens or jumps more than 0.5%
  above the level does not fill: no chasing.
- **Filter:** none on the 50-day average (Sultan, 28 Oct 2026: not an unconditional requirement).
  The breakout itself is the trend signal.
- **Stop-loss:** `stop_atr` × daily ATR(20) below the level, placed with the buy.
- **Take-profit:** 3 × the stop distance (as every strategy), placed with the buy.
- **Strategy exit:** sell at market when a daily close falls below the lowest low of the last
  `exit_days` finished days (the classic breakout exit; it lets winners run).
- **Time stop:** 30 days.
- No new order while the coin is held or its order is waiting.

Every order still goes through `bot.risk.engine.evaluate()`, when it is placed (section 4 covers
what changes when the fill comes later). No risk limit changes: 0.25% risk per trade, at most 3
positions, at most 25% of the account in one coin, the daily and weekly loss limits and the 6%
drawdown breaker.

### Settings

| Setting | Value | Tunable? |
| --- | --- | --- |
| `entry_days` | 20 | yes |
| `stop_atr` | 2.0 (daily ATR(20)) | yes |
| `exit_days` | 10 | yes |
| Limit above the level | 0.5% | fixed |
| ATR, take-profit, time stop | 20 days, 3R, 30 days | fixed |

Three tunable settings, the project's maximum. The defaults are the classic breakout values
(20-day entry, 10-day exit, 2 ATR stop), chosen before any test, not fitted.

### Coins

BTC, ETH, XRP and BNB, with H2b's correlation groups (BTC and ETH together). No other coin.

## 4. Orders placed in advance: what has to be built (after approval)

> **Amended by Sultan's decision 4 (31 Oct 2026):** Binance Spot does not accept a stop-type buy as the
> working (first) order of an OTOCO or OPOCO list: only LIMIT or LIMIT_MAKER (checked in
> `binance-sdk-spot`'s own definitions, OrderListOtocoWorkingTypeEnum and OrderListOpocoWorkingTypeEnum).
> So, as item 1 foresaw, there is no protected advance order on the exchange. Sultan chose instead that
> **the bot holds each daily order itself** (`bot.execution.held`): every round (60 s) the runner checks
> the best ask, and once it trades above the trigger and is still within the limit, it sends the existing
> protected entry (BUY LIMIT FOK at the limit with the stop-loss and take-profit in one list, after a
> fresh `evaluate()`). Protection exists from the first fill, as item 1 required. A held order is one
> attempt, expires at the next 00:00 UTC, and all are dropped when entries are blocked; if the server is
> down, nothing is bought. H5's rules (section 3) are unchanged; only how the order is held changed, so
> it is not a new strategy. Items 3 and 4 become: held orders live in the runner's own file
> (`held-orders.json`), and H5 runs in the same runner, ledger and reconciliation as H2b, so nothing on
> the shared Binance account is unknown to either. H5's trades are sized on at most its own capital
> (`bot testnet|live h5-enable --capital`, Sultan's step).

1. **The order:** Binance's OTOCO list: a waiting buy, and, once it fills, a stop-loss and a
   take-profit attached automatically, so the position is protected the moment it exists. *Verify
   on testnet:* whether Binance accepts a stop-type buy as the waiting leg of OTOCO. If it does not,
   H5 does not trade live: a buy that fills while the bot sleeps would be unprotected for up to
   15 minutes, which the bot does not allow.
2. **The risk check:** run when the order is placed, with the order's worst case (the limit price)
   counted as open risk from then on, so a waiting order already uses up its share of the daily,
   weekly and open-risk limits. When any limit or breaker trips, every waiting order is cancelled
   at once.
3. **The ledger:** a waiting order is recorded before it is sent, with its own state (WAITING),
   and its own client IDs; a fill turns it into an OPEN trade; a cancel or expiry closes it.
4. **Reconciliation:** a waiting order the ledger knows is the bot's own and not a mismatch; any
   other open order still is. Never loosen anything else it checks.
5. **The backtester:** a buy that fills during the day when the high crosses the level, at the
   level or the open if that is higher, never above the limit, plus slippage and fees. An additive
   change: every existing strategy and the H2b forward report replay exactly as before (tested).
6. **Tests and planted bugs** for each part, as for PR-1 and PR-2.

## 5. How H5 is tested

- **Step A, backtest:** the same walk-forward and the same selection rule (all 7 checks) as H2b,
  unchanged, on the research period (test windows 2022-01 to 2025-09; the holdout is spent and never
  used). Compared with D1, H2b and random entries with the same exits. If it fails any check, it is
  dropped, not tuned and re-run.
- **Step B, testnet:** at least 5 clean round trips of the advance order (placed, filled,
  protected, closed), as the testnet gate was for H2b.
- **Step C, forward test on new prices:** simulated money, its own account (`~/forward-h5`), the
  same pass rules as H2b's forward test.
- **Live:** only after A, B and C, and only when Sultan switches it on. It shares the live Binance
  account with H2b, so before going live Sultan decides how the two share the capital (decision 3).

## 6. What it can and cannot do (honest expectations)

- It trades rarely: a breakout to a new 20-day high, a few times a year per coin. Not daily.
- Breakout strategies lose on most trades and win on a few big ones; long losing streaks are normal.
- Nothing here is a result. Whether H5 has an edge after costs is unknown until step A runs on the
  server.

## 7. Decisions for Sultan

1. **Approved (31 Oct 2026):** the rules and settings in section 3, with one order per coin every day.
2. **Approved (31 Oct 2026):** building section 4, testnet first, with the "no OTOCO stop leg, no live
   H5" rule.
3. **Decided (31 Oct 2026): H5's live trial uses 100 USDT,** separate from H2b's 350 USDT. Note, from
   the risk engine's own figures (`bot entries`, 30 Oct 2026): at 0.25% risk, 100 USDT meets Binance's
   5 USDT minimum only when the stop is within about 3-4% of the entry; H5's stop (2 x daily ATR) is
   usually wider, so most H5 orders on a 100 USDT live trial would be refused as below the minimum.
   The backtest, testnet and forward test use their own capital and are not affected. Sultan may
   revisit the amount before live; the risk limits are not loosened to fit it.
4. **Decided (31 Oct 2026): the bot holds each daily order itself** (see the note at the top of section 4),
   because Binance does not accept a protected stop-type advance order. The forward test runs alongside
   live, as for H2b; the testnet check of the held orders comes first.
