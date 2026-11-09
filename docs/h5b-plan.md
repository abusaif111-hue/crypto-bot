# H5b plan: H5's daily breakout on more coins

Status: **DRAFT, written 9 Nov 2026, waiting for Sultan's approval (section 8).** Written before
any price of the new coins was looked at. Once approved, this document is frozen: changing a coin
rule, a setting or a pass rule after that makes it a new strategy.

## 1. Why H5b

H5 (docs/h5-plan.md, frozen) passed walk-forward on BTC, ETH, XRP and BNB and trades live. It
trades rarely: a breakout to a new 20-day high, a few times a year per coin. Sultan asked on
9 Nov 2026 to include more of the largest coins, so the same rules get more chances to trade.

H5's plan names its four coins and is frozen, so more coins is a new strategy. H5b is exactly
H5 with a wider coin list. **H5 itself, H2b and H2b's forward test stay unchanged.**

## 2. What stays exactly as in H5

- Every rule in docs/h5-plan.md section 3: daily candles from 4h candles; one order placed in
  advance per coin per day at the 20-day high, limit 0.5% above it, valid to the next
  00:00 UTC; stop 2 x daily ATR(20) below the level; take-profit 3R; exit on a daily close
  below the 10-day low; time stop 30 days; no 50-day filter.
- The three tunable settings (`entry_days`, `stop_atr`, `exit_days`) and their defaults.
- The code: `bot.strategies.h5_breakout.DailyBreakout`, unchanged (its values stay pinned by
  `tests/unit/test_h5.py`). H5b is the same class with a longer coin list and the name "H5b".
- The costs (docs/h5-plan.md section 2), and doubled costs for the pass rules.
- Every risk limit: 0.25% risk per trade, at most 3 open positions, at most 2 per correlation
  group (R-08), at most 25% of the account in one coin, the daily and weekly loss limits and
  the 6% drawdown breaker. More coins never means more open risk at once.

## 3. The coins

### Candidates (fixed now)

The coins Sultan named on 9 Nov 2026, the largest by market value that day, besides the four
H5 already trades: **SOL, TRX, ZEC, HYPE, DOGE and XMR.** No other coin is added later.

**Honest note on this list:** coins that are among the largest today are partly the ones whose
price went up. That favours them in any backtest (survivorship bias). The rules below cannot
remove that; they only stop coins that were unstable or hard to trade during the test years.

### Coin rules (fixed before any price is looked at)

A candidate joins H5b only if it passes **all** of these, checked once on the server:

1. **Tradeable:** a Binance Spot pair `<COIN>USDT` with status TRADING today (exchangeInfo).
   *Expected from public knowledge, verify on the server:* XMR fails (Binance delisted Monero
   in 2024); HYPE may not have a Binance Spot USDT pair.
2. **Enough history:** 4h candles on Binance starting **on or before 1 Oct 2021**, so H5's 60
   days of indicator history exist before the first test window (Jan 2022). A coin listed
   later has a partial first training window; that is allowed and reported.
   *Expected:* HYPE fails (launched in late 2024).
3. **Stable enough** (the design package's stability screen, as in `bot.backtest.universe`),
   over the screen window **1 Oct 2022 to 1 Oct 2025**: its 30-day volatility was above 2x
   BTC's on no more than 25% of days, and its largest fall from a peak was no worse than 70%.
4. **Liquid enough** (R-11, as in `bot.backtest.universe`): daily trading volume at least
   20 million USDT on at least 95% of days in the same window.

Every coin that passes joins; no cap on the number, and no other ranking. If none passes, H5b
is dropped without a backtest.

**Correlation groups** (R-08, as in `bot.backtest.universe`): a coin whose daily returns
correlate with BTC's at 0.8 or more over the screen window joins "majors" (with BTC and ETH);
otherwise it gets its own group. XRP and BNB keep their own groups.

**Memory:** each coin's 4h history takes about 14 MB in the backtest; the 512 MB server ran
H5 on four coins. Before the run, a one-time check measures whether all passing coins fit. If
they do not, the new coins with the lowest median daily volume are left out, one at a time,
until it fits (BTC, ETH, XRP and BNB always stay). The result is written once
(`h5b-coins.json`) and never re-run or overwritten.

The holdout year (Oct 2025 to Oct 2026) is spent and is never downloaded or read for H5b.

## 4. How H5b is tested

The same walk-forward and selection rule as H5, unchanged: research period 2020 to Sep 2025,
test windows Jan 2022 to Sep 2025 (24-month training, 6-month tests), all 7 checks:

1. positive expectancy per trade after doubled costs;
2. every test window, as deployed, stays below the 6% drawdown breaker;
3. at least 100 out-of-sample trades;
4. not driven by one coin (without the best coin, the rest is positive);
5. not driven by one month;
6. no setting moved by 20% makes the result collapse;
7. beats random entries with the same exits (above their 95th percentile).

**One extra check, because H5b is H5 plus coins:**

8. **The new coins earn their place:** out of sample, with doubled costs, the trades on the new
   coins together have a positive total P&L. If they do not, adding them only dilutes H5, and
   H5b fails even if checks 1 to 7 pass (BTC, ETH, XRP and BNB could carry the total).

If any check fails, H5b is dropped, not tuned and re-run, and H5 carries on unchanged.

**Multiple testing, disclosed:** H5b is the 11th strategy tested on this research period
(H1, H2, H3, H2b, H4, three H1DT setups, D1, H5, H5b). The more strategies are tried, the
likelier one passes by luck. Check 8 and the testnet step are the guard against that here; the
selection rule itself is not changed.

## 5. After the backtest (only if it passes, and only with Sultan's approval)

1. **Testnet first:** H5b replaces H5 in the testnet runner (never both: they share four
   coins). At least 5 clean round trips of the held order, as for H5, before live.
2. **Live:** Sultan's switch only. H5b replaces H5 in the live runner, on the same capital
   (514 USDT today). H2b keeps running beside it, as now.
3. **Smaller orders on volatile coins:** a wide stop gives a small order. At 514 USDT and
   0.25% risk (about 1.29 USDT), a stop more than about 25% from the entry gives an order below
   Binance's 5 USDT minimum, which the risk engine refuses. Nothing is loosened to fit.

A forward test on new prices (as docs/h5-plan.md section 5, step C) can run alongside, if
Sultan wants one.

## 6. What it can and cannot do (honest expectations)

- More coins means more breakouts, so more trades than H5; still not daily.
- Smaller coins swing harder: wider stops, smaller orders, and longer losing streaks are likely.
- Breakouts lose on most trades and win on a few big ones. More coins does not change that.
- Nothing here is a result. Whether the new coins add anything after costs is unknown until the
  backtest runs on the server.

## 7. Work needed after approval (one small module each, with tests and planted bugs)

1. **Coin check** (`bot.backtest.h5b_coins`, `bot backtest h5b-coins`): the candidate list in
   code; rules 1 to 4, correlation groups and the memory check; written once to
   `h5b-coins.json`, never overwritten. It reads only the screen window, never the holdout.
2. **Backtest** (`bot backtest run --strategy H5b`): `DailyBreakout` with the coins from
   `h5b-coins.json` and the name H5b; the 7 checks unchanged plus check 8 in the report.
   `h5_breakout.py` is not changed.
3. **Late-listed coins in the engine:** verify the backtest handles a coin whose candles start
   after the run starts; if a change is needed, it is additive and every existing strategy and
   the H2b forward report replay exactly as before (tested).
4. The runner switch (`h5b-enable`) is built only after a pass and Sultan's approval.

## 8. Decisions for Sultan

1. **The candidates and coin rules** in section 3 (including leaving out coins that fail them).
2. **The pass rules** in section 4: H5's 7 checks unchanged, plus check 8.
3. **What a pass leads to** (section 5): testnet first, then H5b replaces H5 on live with the
   same capital, by Sultan's switch only.
