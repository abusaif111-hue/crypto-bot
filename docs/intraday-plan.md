# Intraday research plan: trend, breakout, range and news-aware methods (15-minute signals)

Status: **APPROVED by Sultan on 24 Oct 2026 (decisions in section 11). Frozen.** Written before any
15-minute data was downloaded. Nothing is built yet. It answers Sultan's request for a spot-only intraday strategy
research module. It is research with simulated money only: nothing here can place an order.

Rules for this plan, as for H2b and H4: every rule and pass condition is fixed **before** any
15-minute data is downloaded or looked at. After approval, changing a rule makes it a new strategy.

## 1. What already exists (and is reused unchanged)

| Requirement | Exists today | Where |
| --- | --- | --- |
| Spot, USDT pairs, long only, no leverage | Yes, enforced everywhere | risk engine, config |
| Completed candles only, no look-ahead | Yes, tested end to end at several cut points | `bot.market.data`, strategy tests |
| Fees, spread, slippage, gaps, pessimistic fills | Yes: 0.1% taker fee, 0.01% half-spread, entry and exit slippage, a stop gapped through fills at the open, a candle touching stop and target counts as the stop | `bot.backtest.replay`, `bot.paper.fills` |
| Every simulated entry through the risk engine | Yes | `SimExchange.submit()` |
| Walk-forward (train, then test on unseen windows) | Yes, 8 windows of 6 months | `bot.backtest.walkforward` |
| Selection rule (7 checks) | Yes | `bot.backtest.selection` |
| Expectancy, drawdown, win rate, average gain and loss, sample size, by regime, by coin | Yes | `bot.backtest.metrics`, `selection` |
| Sensitivity (each setting ±20%), doubled costs | Yes | `selection` |
| Random-entry baseline with the same exits, buy-and-hold, cash | Yes | `random_entry`, `benchmarks` |
| Locked final period | The holdout year (Oct 2025 to Oct 2026), **already spent** by H2 | `bot.backtest.periods` |
| Forward test on new prices (simulated money) | Yes | `bot.forward` |
| Tests: missing data, stale prices, rejected orders, API timeouts, uncertain order status, restarts | Yes, in the existing suite (576 tests): e.g. `test_T_FAIL_001_stale_data_blocks`, `test_broken_or_missing_book_fails_closed`, `test_a_rejected_intent_changes_nothing`, `test_a_duplicate_id_means_it_was_sent_before_find_it`, the reconciliation tests for unknown outcomes, `test_restart_mid_trade_keeps_the_position_and_blocks_entries` | `tests/unit` |

What is **missing**: 15-minute candles, 1-hour context built from them, the three intraday methods, a
news-aware variant, timestamped historical events, a separate validation/final split for this research,
a turnover measure, a 100 USDT budget run, and a market-exit cost choice (section 6).

## 2. Lessons that set the bar

- H3 (4h dips in flat markets) and H4 (hourly drops in an uptrend) both **did not qualify**. H4 lost
  1.51 USDT per trade over 331 out-of-sample trades and did no better than random entries.
- Costs decide short-term results. In the simulator today, **every market sale** (stop, time stop or a
  strategy's own exit) is charged the stop-slippage allowance: 0.5% on BTC, 1.0% on other coins. With the
  0.1% fee each side, a round trip ending in a market sale costs about 0.8% (BTC) to 1.3% (others).
  Section 6 asks Sultan how to treat this.
- This research adds the sixth to ninth ideas tested on the same history. Each one raises the chance that
  one passes by luck, so the bar is raised (section 8).
- A number of trades per day is not a goal. The methods trade when their rules say so.

## 3. Data

- **15-minute candles** (primary signals) for the chosen coins, from 2019-01-01, downloaded with the
  existing checks (gaps, irregular candles, impossible prices).
- **1-hour context** is built from the 15-minute candles by the same "finished period only" rule as the
  daily candles: an hour counts only once its last 15-minute candle has closed.
- **Coin:** BTC only (decision 5, chosen by Sultan to stay on the 512 MB server).
- Shorter intervals than 15 minutes are not used: the costs in section 6 make them harder, and nothing
  in this plan needs them.

## 4. The methods (fixed rules; at most 2 tunable settings each)

Shared by all three:
- Decisions after each 15-minute candle closes, from finished candles only.
- Entry: limit 0.10% above the last close (as every strategy), through the risk engine.
- Stop: `stop_atr` x 1-hour ATR(24) below the entry. Take-profit: 3 x the stop distance (as every
  strategy). Time stop as stated per method.
- 1-hour uptrend: the 1h close is above its 50-hour average, and that average is higher than 6 hours earlier.
- 1-hour range: the 50-hour average moved less than 1% over the last 24 hours.

| Method | Context (1h) | Entry (15m) | Strategy exit | Time stop | Tunable (default) |
| --- | --- | --- | --- | --- | --- |
| **I1 trend-following** | uptrend | the 20-bar average crosses above the 80-bar average | the 20-bar average crosses back below, or the 1h uptrend ends | 24 h | `fast` (20 bars; slow is 4 x fast), `stop_atr` (2.0) |
| **I2 breakout confirmation** | uptrend | a close above the highest high of the previous `lookback` bars, on volume at least 1.5 x its 32-bar average, **confirmed** by the next bar also closing above that level | a close back below the breakout level | 24 h | `lookback` (32 bars = 8 h), `stop_atr` (1.5) |
| **I3 range-bound** | range | the z-score of the close against the last 32 closes falls below −`dip` (first bar only) | the close is back at or above its 32-bar average | 12 h | `dip` (2.0), `stop_atr` (1.5) |
| **N news-aware** | applied to each of I1, I2 and I3 as a separate variant (I1-N, I2-N, I3-N) | no new entry from 2 hours before to 2 hours after a scheduled tier-A release (section 5) | unchanged (open trades are not force-sold; selling costs more) | unchanged | none (fixed) |

Fixed values (the 1.5 x volume, 32-bar windows, 50-hour average, 6-hour slope, 1% range, 2-hour event
window, time stops) are pinned by tests, like H2b and H4.

## 5. News: timestamped events, no future information

- **Covered:** scheduled tier-A releases whose time is announced in advance by the official body: US
  central bank (FOMC) statements, US CPI and US jobs reports. Each event is one row with: event time
  (UTC), type, official source link, and the date its schedule was published.
- **No look-ahead:** at simulated time T, only events whose schedule was published before T are visible.
- **Duplicates** (same type and time) are merged; conflicting rows (same event, different times) stop
  the run until fixed. Both are tested.
- **The calendar is compiled from official archives and every row is checked against its source.** No
  date is entered from memory. If a source cannot be checked, its events are left out and the report says so.
- **Not covered:** unscheduled news (exchange incidents, hacks, ETF approvals, token announcements).
  Doing this without future information needs a licensed historical news archive with original
  publication times (decision 6). Without it, "news-aware" means "aware of scheduled macro releases" only,
  and the report says that plainly.

## 6. Costs and execution

| Item | Main assumption | Sensitivity |
| --- | --- | --- |
| Fee | 0.1% taker on buy and sell (BNB discount is off on the account) | doubled |
| Spread | 0.01% half-spread | 0.05% half-spread |
| Entry | limit at last close + 0.10%; fills at the next open only if the price is within it | doubled entry slippage |
| Stop | stop price minus the stop allowance (0.5% BTC, 1.0% ETH); gaps fill at the open | doubled |
| Other market sales (strategy exit, time stop) | **decision 2** | doubled |
| Budget | 10,000 USDT (comparable with earlier results) **and** 100 USDT (the budget boundary: 5 USDT minimum order, step rounding, 25 USDT per coin at most) | |

Decision 2 matters. Today's simulator charges the full stop allowance on every market sale. That is
deliberately pessimistic for a stop in a fast market, and probably far too pessimistic for a routine
small market sale of BTC or ETH, but this has **not been measured**. With it, a method that exits at
market several times a day is very likely to fail on costs alone.

## 7. Periods (training, validation, final)

The holdout year (Oct 2025 to Oct 2026) is spent and stays locked (CLAUDE.md). Within the research period:

| Part | Period | Use |
| --- | --- | --- |
| Training and validation | 2020-01-01 to 2024-10-01 | Walk-forward: settings chosen on 24 months, then tested on the next 6 unseen months; 6 validation windows (2022-01 to 2024-09) |
| **Final out-of-sample** | 2024-10-01 to 2025-10-01 | **Locked by code** for this research. Opened once by Sultan, for all candidates in one run; a second opening is VOID |
| Real test | new prices after Sultan starts a forward test | Simulated money; only for a method that passed the final period |

**Honest caveat:** the final period was part of H1 to H4's walk-forward test windows, so I have seen how
*other* strategies did there. None of these methods has been run on it, but it is not perfectly clean.
That is why passing it leads only to a forward test, never straight to real money.

## 8. Pass rules (all must pass; fixed now)

On the validation windows (walk-forward) and then the final period:

1. Positive expectancy per trade after doubled costs.
2. Every validation window, as deployed, stays below the 6% drawdown breaker.
3. At least 100 out-of-sample trades in validation, and at least 30 in the final period.
4. Not driven by one coin or one month.
5. No collapse when a setting moves 20%.
6. Beats random entries with the same exits above their **99th** percentile (raised from 95th because
   this is the sixth to ninth idea: decision 4).
7. Beats its own **context-only baseline**: entering whenever its 1h context starts (uptrend for I1 and
   I2, range for I3), with the same exits. This shows whether the 15-minute signal adds anything.
8. For the N variants: shows the filter's effect against its parent method on the same windows. N is
   reported, but it passes only by rules 1 to 7 on its own.

Every report also shows: net expectancy, maximum drawdown, win rate, average gain and loss, number of
trades, **turnover** (traded value per year as a multiple of average equity), fees as a share of gross
profit, results by regime, the 100 USDT budget run, and how many trades the 100 USDT budget could not
place (minimum order).

## 9. Safety: the order path stays out of reach

- The research module uses only `SimExchange`. It cannot import `bot.execution` or
  `bot.exchange.trading` (an architecture test enforces this, as for the alerts and dashboard).
- Nothing in this plan changes the live runner, its key, its switch or its cap. Whether live H2b keeps
  trading during the research is decision 1.
- Tests never trade (made-up data and fake exchanges).

## 10. Changes (small pull requests, each with tests; every one adds files or options with the old default)

| # | Change | Existing behaviour |
| --- | --- | --- |
| 1 | 15-minute interval: `bot backtest fetch --interval 15m`; finished-hour resampling (`bot.analysis.resample`, a new function; `daily()` untouched) | unchanged |
| 2 | Research periods and the locked final window (`bot.research.periods`, new), like the holdout lock | unchanged; the holdout lock stays |
| 3 | Costs: an optional market-sale slippage in the cost model (only if decision 2 is B; default = today's), turnover metric, 100 USDT run | unchanged by default |
| 4 | I1, I2, I3 and the context-only baselines (`bot.strategies.intraday_*`, new) | unchanged |
| 5 | Event calendar (`data/events/*.csv` with a source per row) and the N variants | unchanged |
| 6 | Memory measurement (as for H4) and `bot research run` / `bot research final` (Sultan only) | unchanged |

**Memory and time (estimates, measured in step 6 before any run):** 1h data for BTC and ETH peaked at
159 MB. 15-minute data has 4 times as many candles, so BTC and ETH probably need well over 400 MB,
**more than the 512 MB server has free (about 260 MB)**. Running six candidates will take hours (H4 on
1h took about 35 minutes; 15m with six candidates may take over 10 hours). See decision 5.

**Rollback:** every change adds new files or an option whose default is today's behaviour. To roll
back, stop any research run (`pkill -f "bot research"`), then `git checkout <tag before the change>` and
`.venv/bin/pip install -e .`. No service uses the new modules, so no account is affected.

**Known limits:** backtests do not predict future profits; past order books are not available (depth is
assumed); spreads widen in fast markets more than the model shows; survivorship bias (only coins that
still exist); unscheduled news is not covered without a licensed archive; the final period is weakly
contaminated (section 7).

## 11. Sultan's decisions (24 Oct 2026)

Sultan chose decisions 1, 2 and 5 himself; for 3, 4, 6 and 7 he accepted the recommended answers.

- [x] 1. **Live H2b during this research:** (a) keep running. The research cannot reach it.
- [x] 2. **Market sales other than stops:** (B) 0.05% slippage for those sales; stops keep their full
      allowance; today's model (full allowance on every market sale) is always shown as a sensitivity.
      These results are less pessimistic than H1 to H4's, so they are not directly comparable.
- [x] 3. **Periods:** the training/validation split and the locked final window in section 7.
- [x] 4. **Random-entry bar at the 99th percentile.**
- [x] 5. **Coins and server:** (c) BTC only, on the 512 MB server. Consequences, fixed now before any
      data is seen:
      - pass rule 4 checks one month only (with one coin there is no "best coin" to remove);
      - BTC alone may give fewer than 100 validation trades for a method; if so, that method fails;
        no coin is added afterwards;
      - memory is measured first (as for H4). If BTC on 15m from 2019 does not fit even with Claude
        Code stopped on the server during the run, the research stops and Sultan decides again; the
        period is not shortened automatically.
- [x] 6. **News:** (a) scheduled official releases only (free).
- [x] 7. The methods and pass rules in sections 4 and 8 are approved.
