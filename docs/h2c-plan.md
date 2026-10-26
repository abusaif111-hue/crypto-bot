# H2c research protocol: do more coins give H2b more good trades?

Status: **DRAFT, revision 3 (complete research protocol), for Sultan's review, 26 Oct 2026. Not approved.
Nothing is built, no screen has run, no backtest has run.**

The objective is to decide whether the proposed coin-list change **deserves further investigation**, not to prove
that H2c is better than H2b. If the evidence is insufficient or a critical data-quality check fails, the study says
so and stops; no criterion is relaxed.

**Freezing.** When Sultan approves this protocol, its approving commit freezes every rule, source, date, method
and threshold below. After that, any change, made before or after seeing a result, is not an amendment: it is a
**new exploratory experiment**, declared as such in its own document and in the register of section 12, and it
counts toward the number of tests (section 9.1).

**Approvals, in stages (each needs Sultan's explicit decision; none follows from an earlier one):**

| Stage | What happens | Needs |
| --- | --- | --- |
| A | This protocol is reviewed and frozen | Sultan approves it |
| B | Code for section 13 is built and reviewed; then the data checks (section 4) and the coin screen (section 5) run once on the server; results reported | Sultan approves the code, then the run |
| C | Sultan reviews the screen's coins and the data-check report | Sultan's decision to continue |
| D | The Phase 1 backtest (sections 6 to 10) runs once; results reported | Sultan's decision to run it |
| E | Any further simulation (a forward test) | A separate plan and decision; not part of this protocol |

## 1. Scope: what stays exactly as it is

H2b (code `h2b_blend.py` and `strategies/base.py`, settings, coins, its forward test and account `~/forward-h2b`);
`config/default.yaml`; `CEILINGS` and `FLOORS`; `evaluate()` and `_verify()`; order validation and protected entries;
reconciliation and unknown-order handling; balance checks and accounting; data-freshness checks; key-permission
checks; the live switch; live capital (100 USDT) and its ceiling; risk per trade (0.25%); position, exposure, loss,
drawdown, liquidity and correlation limits; production services. No live trading, no real orders, no new service.

## 2. Baseline: H2b, unchanged (`docs/h2b-plan.md`, frozen)

Daily candles from 4h candles; one decision a day at the first 4h close after 00:00 UTC; finished candles only.
`vol` = 30-day volatility of daily closes; for L in 14, 28, 56: `r_L` = close / close L days ago - 1, strong when
`r_L` > `threshold` x `vol` x sqrt(L). Entry: 2 of 3 strong and `vol` below the 80th percentile of the coin's 30-day
vol over the previous 365 days. Limit buy 0.10% above the last close; stop `atr_mult` x daily ATR(14) below; take-profit
3x the stop distance; time stop 30 days; exit when 2 of 3 returns are below zero. Tunable `threshold` and `atr_mult`.
Coins BTC, ETH ("majors"), XRP, BNB (own groups). Known: walk-forward 104 out-of-sample trades, +1.90 USDT per trade
with doubled costs; forward test day 3, 0 trades (not evidence). Declared: XRP and BNB were chosen with 2022-2025
data that H2b's walk-forward also traded (look-ahead in the baseline's own coin choice).

## 3. Question, hypotheses, primary outcome

**Question.** With H2b's rules, settings grid, risk limits and correlation controls unchanged, and only up to 2 extra
eligible coins chosen without hindsight, are there more trades with positive net expectancy after doubled costs,
without more drawdown and without dependence on one coin, cluster, month or regime?

- **H0:** the extra coins' trades have net expectancy after doubled costs of zero or less.
- **H1:** they are positive, and H2c's drawdown is not materially worse than H2b's on the same prices.
- **Primary outcome:** the extra coins' out-of-sample net expectancy per trade after doubled costs, with its 90%
  interval (section 9.2). Everything else is a guard or a description.

## 4. Data: sources, dates, checks

### 4.1 Sources

- **Candles:** Binance's public archive, monthly 4h spot klines,
  `https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/4h/{SYMBOL}-4h-{YYYY-MM}.zip`, each with its
  `.CHECKSUM` (SHA-256), for **every** coin in Phase 1, H2b's four included, so H2b and H2c use identical data.
  Daily-file listings (`data/spot/daily/klines/`) are used only to find missing days.
- **Pool and rules:** the archive's file listing (which pairs had data when) and Binance's `exchangeInfo`, which also
  lists delisted pairs with status `BREAK`, as an independent cross-check, and gives each pair's last known filters.
- **Never:** any data from 2025-10-01 or later in Phase 1 (section 4.3); any H2b or H2c forward-test result.

### 4.2 Date handling

- All times UTC. A day is 00:00 to 24:00 UTC; weeks start Monday 00:00 UTC; months are calendar months.
- Archive timestamps are milliseconds or microseconds (Binance changed spot files to microseconds from 2025);
  the loader detects the unit per file and converts to milliseconds. A 4h candle is accepted only if its open time
  is a multiple of 4 hours and its close time is open + 4h - 1 ms.
- Daily candles come from 4h candles with the existing `bot.analysis.resample.daily` (unchanged).
- **Listings:** a pair's history is split wherever 3 or more consecutive days are missing; each piece is a separate
  listing with its own listing date (first day of the piece). So a re-used ticker (for example LUNAUSDT, missing
  14-30 May 2022) or a halted and relisted pair (FTTUSDT) is never treated as one continuous coin.
- **Trading at T:** a listing is trading at T if it has a daily candle for the day before T.

### 4.3 The holdout and later data

The research period is **2020-01-01 to 2025-09-30** (warm-up from 2019-01-01). The spent holdout year
(2025-10-01 to 2026-10-01) is never downloaded, read or used, for the screen, the backtest or the checks; the engine's
existing guard (`assert_no_holdout`) refuses any candle that closes on or after 2025-10-01. Nothing after 2025-09-30
is used at all in this protocol.

### 4.4 Data-quality checks (run in stage B, before the screen; reported in full)

| # | Check | If it fails |
| --- | --- | --- |
| DQ-1 | Every archive file used matches its `.CHECKSUM` | Download again once; still failing: **stop** |
| DQ-2 | For BTC, ETH, XRP, BNB, every 4h candle of the research period in the archive equals the API candles (`bot backtest fetch`) | Any difference: **stop** |
| DQ-3 | Every USDT pair in `exchangeInfo` (any status) with a candle in the research period is in the archive, and vice versa | Report each missing pair; if more than 5% of the pool at any screen date is missing, **stop** and report "survivorship bias not removed" |
| DQ-4 | Missing days per listing inside each screen window | More than 2% missing in its window: that listing is ineligible at that date (not a stop) |
| DQ-5 | Listings split at gaps of 3+ days (4.2) | Reported per pair |
| DQ-6 | Timestamp unit per file detected and converted | Any file with an undetectable unit: **stop** |
| DQ-7 | No candle at or after 2025-10-01 is loaded | **Stop** |
| DQ-8 | Each eligible pair has exchange filters (tick, step, minimum order) from `exchangeInfo` (last known for delisted pairs) | Missing: that pair is ineligible |

A **stop** ends the study at stage B with a report; no rule is changed to get past it.

### 4.5 Completeness found so far (preliminary, 26 Oct 2026)

Done from the cloud container by **file names only** (no prices downloaded), so it is not the stage-B check; it shows
the archive is usable. See the appendix: 705 USDT pairs (leveraged tokens excluded) have monthly 4h files; **199 stop
before 2026-09** (delisted or renamed), and their files run up to the month they stopped, so delisted pairs are kept;
7 pairs have whole months missing (CVC, FTT, VEN and four stablecoins); day-level gaps also occur (LUNAUSDT,
14-30 May 2022), which is why listings split at 3-day gaps. Not yet verified: that the archive holds *every* pair that
ever traded (DQ-3, against `exchangeInfo`).

## 5. The coin screen (frozen rules)

Run once per screen date T, using only data before T. It never uses returns, profits or any strategy or backtest
result.

**Screen dates:** T = 2020-01-01, then every 6 months: 2020-07-01, 2021-01-01, ..., 2025-07-01 (12 dates; from
2022-01-01 they are the walk-forward test-window starts). At any time t, the eligible extra coins are those picked at
the latest T at or before t. (For the forward-test date, see section 11; it is not run in this protocol.)

At each T:

1. **Pool:** every USDT spot listing trading at T (4.2), excluding H2b's four coins; stablecoins, fiat and gold
   (`EXCLUDED_BASES` in `bot.backtest.universe`, unchanged); wrapped and staked coins (same list); leveraged tokens
   (bases ending UP, DOWN, BULL, BEAR); and any pair failing DQ-4 or DQ-8.
2. **Listing:** the listing's first daily candle is at least **4 years before T** (3-year window + 1 year of trading).
3. **Screen window** `[T - 3 years, T)`. Unchanged rules over it:
   a. 30-day volatility above 2x BTC's on at most 25% of days;
   b. worst fall from a peak at most 70%;
   c. at least 20 million USDT traded on at least 95% of days.
4. **Pick:** up to **2** passing coins, highest median daily quote volume in the window first; ties broken by symbol
   name. (Sultan's memory cap: at most 4 coins beyond BTC and ETH in total; with XRP and BNB, at most 2 more.)
5. **Correlation group (R-08 as written):** correlation of daily returns with BTC over the window of 0.8 or more:
   "majors" (with BTC and ETH); below 0.8: the coin's own group.

**Output, written once and never re-run:** per T, the pool size, the count failing each rule, the picked coins, their
groups and the numbers behind each decision, and the DQ report; added to this document as an appendix for stage C.
If no extra coin is ever picked at any T, the study ends at stage C.

**Residual biases, declared:** rule 3b (worst fall) and the volume ranking use only past data, but can favour coins
that had done well before T.

## 6. The backtest procedure (stage D)

- **Engine:** the existing backtester (`bot.backtest.engine.run`, `SimExchange`, `evaluate()`, `config/default.yaml`),
  unchanged. Two modes: *as deployed* (the 6% breaker halts) and *edge* (breach recorded, peak reset), as today.
- **Walk-forward:** the existing windows (`bot.backtest.walkforward`): 24-month training, 6-month test, test windows
  from 2022-01-01 to 2025-09-30 (the last one shortened). Settings chosen in each training window by the existing
  rule (`choose`: every combination of `threshold` and `atr_mult` at 80/100/120%, best training score), unchanged.
- **Universe per moment:** H2b: BTC, ETH, XRP, BNB always. H2c: those four plus the extra coins eligible at that
  moment (section 5), in training and test windows alike.
- **Capital:** every run starts at 10,000 simulated USDT (`STARTING_QUOTE`): each walk-forward window separately, and
  the full-period runs from 2020-01-01. Identical for H2b and H2c.
- **Execution assumptions (the simulator's, unchanged, identical for both):** entries are limit buys at 0.10% above
  the last close, filled at the next price plus half-spread and entry slippage, through `evaluate()`; within a candle,
  a candle that opens below the stop fills at the open (gapped), one that touches the stop fills at the stop, and one
  that trades through the take-profit fills at the take-profit; a candle touching both counts as a stop; every exit
  other than the take-profit sells at market with the coin's stop-slippage allowance.
- **A listing that stops while a position is open** (its data ends or a 3-day gap starts) is closed at **zero**
  (the whole position lost), the most cautious assumption; the report also shows, for information only, the result
  if it were sold at the last close.
- **Runs:** H2b and H2c, each: the walk-forward at normal costs, at doubled costs, and the full-period runs and
  checks of `bot.backtest.selection`. Then the random-entry runs (section 9.1).

## 7. Performance definitions (frozen)

- **Trade net P&L:** `ClosedTrade.pnl` = sale proceeds after the exit fee minus purchase cost including the entry fee,
  on the quantities actually filled (rounded down to the step).
- **Fees:** 0.1% taker fee on every fill, entry and exit (also on take-profits, which would be maker fees in reality:
  cautious). Reported as the sum of `Fill.fee`.
- **Spread:** half-spread 0.01% on each side of the price. **Slippage:** entries 0.05%; market exits at the coin's
  stop allowance (BTC 0.5%, ETH 1.0%, every other coin the config default 1.0%). Reported as the sum of
  `Fill.slippage`.
- **Doubled costs:** a separate simulation with `CostModel.doubled()`: fee 0.2%, half-spread 0.02%, every slippage
  allowance doubled. Not an arithmetic adjustment: the trades are re-simulated.
- **Net expectancy:** mean trade net P&L over closed trades, in USDT, and in units of planned risk (mean of
  `pnl / planned_risk`).
- **Out-of-sample:** trades opened in the test windows of the walk-forward, in edge mode (as `selection.evaluate`).
- **Maximum drawdown:** on the equity marked at every 4h close (coins at the best bid), the largest fall from the
  running peak (starting at the starting capital): `max(1 - equity / peak)`, per test window as deployed; the gate uses
  the worst window.
- **Sharpe / Sortino:** from equity at each UTC day's end: daily returns r; Sharpe = mean(r) / sd(r) x sqrt(365);
  Sortino = mean(r) / sqrt(mean(min(r, 0)^2)) x sqrt(365); risk-free rate 0.
- **Return / max drawdown, time in market** (share of 4h marks with a position), **win rate** (information only).
- **Regimes:** `bot.analysis.regime.regime_labels` on each coin's own candles (unchanged): bull, bear, sideways, and
  high volatility. A trade takes the label of the last full day before its entry. (The high-volatility cut uses the
  coin's whole research period, a look-ahead in the *label*, used only for reporting.)
- **Clusters:** trades in the same correlation group whose holding periods overlap, chained, form one cluster.
- **Extra-coin trades:** H2c's trades on coins other than BTC, ETH, XRP and BNB.
- **H2c minus H2b:** total out-of-sample net P&L of H2c minus that of H2b at doubled costs. This counts both the extra
  trades and any trades H2c lost or changed because extra coins took position slots.

## 8. What 30 trades can and cannot show (assumptions stated)

- Planned risk per trade on 10,000 USDT at 0.25%: 25 USDT (written R). H2b's doubled-cost walk-forward expectancy,
  +1.90 USDT, is about **0.08 R**.
- **Assumed** spread of one trade's result: outcomes run from about -1 R (stop) to +3 R (take-profit). If a share w of
  trades win +3 R and the rest lose -1 R, the standard deviation is 4 R x sqrt(w(1-w)): 1.6 R at w = 0.2, 1.8 R at
  w = 0.3. The illustration uses **1.5 R (about 37 USDT)**, slightly optimistic. Phase 1 reports the real figure.
- **Assumed independence**, which overstates the information: trades in the same cluster move together. With average
  cluster size m and within-cluster correlation rho, the variance grows by about 1 + (m - 1) x rho (for example, m = 1.5
  and rho = 0.5 give 1.25, so 30 trades carry about 24 trades' information). Regimes add further dependence: trades
  bunch in rising markets.
- With those assumptions, 30 independent trades give a standard error of about 37 / sqrt(30) = **6.8 USDT**, a 95%
  interval of about **plus or minus 13 USDT** around an expectancy near +2: **30 trades cannot tell a small edge from
  none.** They only show whether a strategy is clearly broken.
- Showing that the extra trades are positive at 0.2 R (5% one-sided, 80% power) would need about 350 independent trades;
  showing H2c beats H2b by that margin, about 880 per strategy. Neither exists in this data.
- **Labels used in every report:** *exploratory* (anything from Phase 1: reused data); *not demonstrated* (an interval
  whose lower end is at or below zero); *statistically demonstrated* (only if an interval excludes zero **and** the test
  survived the multiple-testing rule of 9.1; even then, on reused data, it is exploratory evidence, not confirmation).

## 9. Statistical methods (frozen)

### 9.1 The random-entry test

- **Null hypothesis:** H2c's entry timing has no information: its expectancy per trade is a draw from the distribution
  of random entries with exactly the same exits, coins and risk rules.
- **Random entries:** `RandomEntry.with_exits_of(H2c, ...)` (unchanged): at every finished 4h candle, for every coin
  in the full candidate list in symbol order, one draw u from `random.Random(seed)` (Python's Mersenne Twister); if
  u < 1/30 and the coin is eligible at that moment, a buy idea with H2b's own stop, take-profit, time stop and exits,
  through `evaluate()` like any idea. (Drawing for ineligible coins too keeps each seed's sequence independent of
  the universe.) The entry rate is fixed (about one idea per coin every 5 days), not matched to H2c's.
- **Seeds:** 0, 1, ..., N - 1 with **N = 2,000**, all run; results repeat exactly.
- **Test statistic:** E = mean net P&L per closed trade over the full research period (2020-01-01 to 2025-09-30), in
  edge mode, at normal costs, with H2c's default settings (`threshold` 0.5, `atr_mult` 3), as check 7 of
  `bot.backtest.selection` does today. E_s for each random seed s.
- **p-value:** p = (1 + #{s : E_s >= E}) / (N + 1).
- **Multiple testing:** m = the number of strategy runs evaluated on this research period, H2c included: H1; H2 on BTC
  and ETH; H2 on four coins; H3; H2b; H4; H2c: **m = 7**. Family-wise level 5%, Bonferroni: each test at
  **alpha = 0.05 / 7 = 0.00714** (the 99.29th percentile). Any further run on this period, including a new exploratory
  experiment under this protocol, raises m and is reported with the new alpha.
- **Monte Carlo uncertainty:** with c = #{s : E_s >= E}, a 95% Clopper-Pearson interval for the true p is reported.
  The decision uses it, so Monte Carlo noise cannot flip it:
  - **pass** only if the interval's upper end is below alpha: **c <= 6** of 2,000;
  - **fail** if its lower end is above alpha: **c >= 23**;
  - **inconclusive** for c = 7 to 22: reported as such and **treated as not passing**. The run is not extended after
    seeing the result (that would be optional stopping); a larger N would be a new exploratory experiment.
- **Is N = 2,000 adequate?** If the true p were half of alpha, a conclusive pass would come with probability 0.43; at a
  quarter of alpha, 0.93; at alpha itself, 0.01. So N = 2,000 rarely passes a strategy by Monte Carlo luck, but can
  miss a real but modest effect. N = 1,000 would need c <= 1 and is not adequate. Runtime is measured on 10 seeds first;
  if 2,000 runs do not fit the server, the test is reported **not run** and the gate cannot pass.
- **How well it is calibrated:** conservative where the 7 runs are alike (correlated results); **not** conservative about
  forks it cannot count (section 12). It tests entry timing on all coins, not the coin addition itself; that is 9.2's job.

### 9.2 Intervals: block bootstrap

- **Unit:** calendar month of the trade's entry (trades last up to 30 days and regimes last months, so whole months are
  resampled together; this also keeps trades of one cluster together in most cases).
- **Method:** 10,000 resamples of the out-of-sample months with replacement, `random.Random(20261026)`. Per resample:
  expectancy = sum of net P&L / number of trades in the drawn months. 90% interval: the 5th and 95th percentiles.
- **Paired difference:** H2c minus H2b uses the same drawn months for both.
- For information only: the same with quarter-year blocks.

## 10. The Phase 1 gate (all must hold; fixed now)

1. The 7 checks of `bot.backtest.selection`, unchanged, except check 7, which is replaced by the conclusive pass of 9.1.
2. **Net expectancy:** H2c's out-of-sample expectancy after doubled costs is positive and at least 75% of H2b's.
3. **Drawdown:** H2c's worst test window, as deployed, at most H2b's plus 1 percentage point, and below 6%.
4. **Primary outcome:** the extra-coin trades' expectancy after doubled costs is positive, and H2c minus H2b is
   positive. Both with 90% intervals; if a lower end is at or below zero, "not demonstrated" is printed beside it.
5. **Robustness:** rules 2 and 4 hold in both halves of the out-of-sample period (2022-01 to 2023-10, 2023-11 to
   2025-09), and in each regime with at least 10 trades; H2c stays positive without its best coin, its best cluster and
   its best month.
6. **Research target, not evidence:** at least 25% more completed out-of-sample trades than H2b. Missing it means H2c
   does not do what it was built for. Meeting it proves nothing; rules 1 to 5 decide.

**Outcomes:** *fails* (any rule fails, or a stage-B stop); *inconclusive* (9.1 inconclusive, everything else passing):
treated as not passing; *passes the exploratory gate* (all hold): H2c **deserves further investigation**, nothing more.
In every case the result is reported in full and recorded in `RESEARCH`; H2b continues unchanged.

## 11. After the gate (not part of this protocol)

A pass does not start anything. A forward test would need its own plan and Sultan's separate decision. If one is ever
proposed: its coins would come from the screen at T = 2025-10-01 (data 2022-10-01 to 2025-09-30, listing by
2021-10-01), never using the holdout year; it would run in its own simulated account, with H2b and H2c replayed on the
same recorded candles, and the limits of section 8 would apply in full.

## 12. Register of choices influenced by earlier results (declared)

| Choice | Influenced by |
| --- | --- |
| Testing more coins at all | H2 needed more coins to reach 100 trades; H2b quiet for 4 days (`bot entries`, 26 Oct) |
| Listing rule "4 years before T" instead of the fixed 2019-10-01 | Made **after** seeing that the first screen picked only 2 of 4 possible coins (XRP, BNB). Rationale (one fixed coin list is not needed with a rolling screen; no coin judged on its first year) written afterwards |
| Point-in-time pool from the archive | A known bias of the first screen (survivorship); a correction, but chosen after its result |
| C3 first; C1 and C2 deferred | Revision 1's reasoning and Sultan's review |
| Random bar 99.29th, N = 2,000, inconclusive band | Calibrated to the 6 earlier runs |
| Splitting listings at 3-day gaps | The archive check of 26 Oct (LUNAUSDT, FTTUSDT), done before any price was seen |

Every Phase 1 result is therefore **exploratory**, not an independent confirmation.

## 13. Work needed at stage B (after Sultan approves the code; one small module each, with tests)

1. Archive loader with checksums, unit detection, gap splitting, and the DQ-1 to DQ-8 report.
2. The point-in-time screen (section 5), one write, never re-run.
3. Walk-forward with a universe per moment; H2b and H2c on identical data.
4. Phase 1 report: the definitions of section 7, clusters, the bootstrap of 9.2, the random test of 9.1 with m, alpha,
   c and its interval, and the gate of section 10 with the labels of section 8.

Results come only from runs on the server.

## 14. Limitations

Reused research data (6 earlier runs); post-hoc choices (section 12); survivorship bias removed only as far as DQ-3
confirms; ranking and worst-fall rules can tilt toward past winners; today's or last known exchange filters used for
the past; correlated coins (fewer independent trades than the count); a mostly rising 2020-2025 market; modelled costs
(cautious, but real fills differ, and smaller coins may have wider spreads, which the risk engine refuses above 0.10%);
the 512 MB server limits coins and random runs.

## 15. Sultan's decisions (stage A)

- [ ] Approve and freeze this protocol (sections 1 to 14), including the data checks and stop rules (4.4), the screen
      (5), the definitions (7), the statistical methods (9) and the gate (10).
- [ ] Separately, later: approve the stage-B code; then the stage-B run; then stage C; then stage D.

---

## Appendix: archive completeness, preliminary (26 Oct 2026)

From the cloud container, reading the archive's file listing only
(`s3-ap-northeast-1.amazonaws.com/data.binance.vision`); no candle data was downloaded and no screen rule was applied.

- 3,745 symbols in the monthly spot archive; 757 end in USDT, 705 after excluding leveraged tokens.
- Of the 705: 1 has no 4h files; **199 have 4h files that stop before 2026-09**, by the year they stop: 2018: 2,
  2019: 3, 2020: 8, 2021: 4, 2022: 26, 2023: 18, 2024: 40, 2025: 51, 2026: 47.
- 7 have at least one whole month missing inside their history: CVCUSDT, FTTUSDT, TUSDUSDT, USDCUSDT, USDPUSDT,
  USDSUSDT, VENUSDT.
- Spot checks (4h and 1d monthly files): XMRUSDT 2019-03 to 2024-02; WAVESUSDT and OMGUSDT to 2024-06; USTUSDT
  2021-12 to 2022-05; ANCUSDT 2022; BTTUSDT 2019-01 to 2022-01; SRMUSDT, BTCSTUSDT, MIRUSDT to 2022-11/12; HNTUSDT to
  2022-10; FTTUSDT missing 2022-12 to 2023-08 (daily files stop on 2022-11-15); LUNAUSDT monthly files continuous,
  but daily files missing 14-30 May 2022 (a re-used ticker).
- Not yet verified: completeness against `exchangeInfo` (DQ-3), checksums (DQ-1), equality with the API (DQ-2).
