# H2c research protocol: do more coins give H2b more good trades?

Status: **DRAFT, revision 7 (final for stage A), for Sultan's review, 26 Oct 2026. Not approved.
Nothing is built, no screen has run, no prices have been downloaded, no backtest has run.**

The objective is to decide whether the proposed coin-list change **deserves further investigation**, not to prove
that H2c is better than H2b. If the evidence is insufficient or a critical data-quality check fails, the study says
so and stops; no criterion is relaxed.

**Freezing.** When Sultan approves this protocol, its approving commit freezes every rule, source, date, method
and threshold below. After that, any change, made before or after seeing a result, is not an amendment: it is a
**new exploratory experiment**, declared as such in its own document and in the register of section 12, and it
counts toward the number of tests (section 9.3).

**What stage A approval means, and does not.** Approving stage A freezes this text. It does **not** approve any
implementation, any download of prices, the coin screen, coin selection, a backtest, a forward test, a new service
or any live trading. Each of those needs its own explicit decision (table below).

Revision 4 (after Sultan's review of revision 3): a second, matched random benchmark and an assessment of what the
random test can and cannot show (9.1, 9.2); the multiple-testing family justified, with earlier selection and informal
experiments accounted for (9.3); the screen report records every candidate (5.1); the bootstrap with few months,
clusters and regimes, and minimum-evidence rules (9.4, 10); a list of unresolved limitations (16).

Revision 5 (final clarifications): exactly how R2 draws its entry days, matches the number of ideas and applies the
exit-vote exclusion, why that uses no future information or favours H2c, and what stays unmatched (9.1a); what the
0.05 / 7 threshold does and does not control (9.3); and what passing the gate does and does not mean (10.1).

Revision 6 (R2's statistics): S/Q is calibrated in sample, which makes R2 a conditional test; which constraints the null
keeps and which it does not; R2's spread is likely too narrow for H2c's bunched entries (anti-conservative), with a
check reported beside every p-value; and everything fixed before results, with no further runs (9.1b).

Revision 7 (approach A, chosen before any result): R1 and R2 are exploratory diagnostics and a hurdle, never evidence
of a statistically significant entry edge; what they can and cannot support is stated in 9.1c; no result of this
protocol is labelled statistically significant or statistically demonstrated.

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
  interval (section 9.4). Everything else is a guard or a description.

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

**Frozen thresholds** (pinned by the screen code's tests, as H2b's values are): listing 4 years; window 3 years;
volatility multiple 2x BTC on at most 25% of days; worst fall at most 70%; 20 million USDT on at least 95% of days;
missing days at most 2%; gap split at 3 days; at most 2 picks; correlation 0.8. **No threshold may be relaxed, no rule
dropped, no coin added or removed by hand, and the screen is never re-run to obtain more coins.** If too few coins
pass, that is the result.

### 5.1 The screen report (written once, never re-run)

For **every** USDT listing trading at **every** screen date T, whether in the pool or excluded from it, one row:

- symbol, listing number (after gap splitting), listing date, last date with data, and why it is in or out of the pool
  (excluded category, DQ-4 missing-day share, DQ-8 filters);
- every screening metric with its value: days of data in the window; share of days with 30-day volatility above 2x
  BTC's; worst fall from a peak; share of days with at least 20 million USDT traded; median daily quote volume;
  correlation of daily returns with BTC;
- pass or fail for each rule, and the **first rule it failed**; its rank by volume among passing coins; picked or not,
  and if not picked, why (rank beyond 2);
- for each picked coin: its correlation group and the first T at which it became eligible.

Also per T: the pool size, the count excluded per category, the count failing each rule, and the picked coins. Per
coin across all T: the dates it was picked and dropped. The report is a JSON file plus a readable table, committed
under `docs/h2c-screen/` **before** stage C; this document's appendix records their SHA-256 and a per-T summary.
Nothing in them is edited later.

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
  checks of `bot.backtest.selection`. Then the random-entry runs of both benchmarks (sections 9.1 and 9.3).

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
- **Active month:** a calendar month in which at least one out-of-sample trade was opened. Reported for H2b, H2c and
  the extra-coin trades separately, with the number of clusters and the trades per month.
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
- **Reaching a minimum trade count is never evidence of profit or superiority.** A minimum only decides whether a
  result is reported as judged or as "insufficient evidence" (section 10).
- **Labels used in every report:** *exploratory* (anything from Phase 1: reused data); *not demonstrated* (an interval
  whose lower end is at or below zero); *shown on reused data* (an interval of 9.4 excludes zero; still exploratory,
  not confirmation). **No result of this protocol is labelled "statistically significant" or "statistically
  demonstrated"**: every result uses reused data, and the random comparisons are diagnostics (9.1c).

## 9. Statistical methods (frozen)

### 9.1 Two random-entry benchmarks

Both answer: does H2c's *choice of entry days* add profit per trade, compared with entering at random under exactly
the same exits, coins and risk rules? They are **exploratory diagnostics and a hurdle**, not tests of a statistically
significant edge (9.1c). H2c must clear the hurdle on **both** (section 10).

**Common to both.** Exits are H2b's own (`RandomEntry.with_exits_of`): the 3x ATR stop, the 3x take-profit, the 30-day
time stop and the daily momentum exit vote. Every idea goes through `evaluate()` like any other, so sizing, the 3-position,
1-per-coin and 2-per-group limits and every other rule apply. Only coins eligible at that moment can be bought (H2b's four
plus the extra coins eligible then). Costs, period, mode and settings are those of the test statistic (9.3).

**R1, the existing benchmark (kept for continuity with the six earlier runs).** At every finished 4h candle, for every
coin in the *full candidate list* (H2b's four plus every extra coin picked at any T), in symbol order, one draw u from
`random.Random(seed)`; if u < 1/30 and the coin is eligible, it is a buy idea. About one idea per coin every 5 days, at
any of the six daily 4h closes. Seeds 0 to 1,999. (Drawing for ineligible coins too keeps each seed's sequence
independent of the universe.)

**R2, the matched benchmark (new in revision 4).** Built to remove R1's known tilts (9.2). Exact rules in 9.1a.

#### 9.1a R2, exactly

- **Decision moments.** Only H2c's own: the first finished 4h candle after 00:00 UTC each day (the moment H2b's
  `_new_day` is true), nothing in between.
- **Qualifying coin-days.** At a decision moment, coin c qualifies when all hold, each computed only from candles that
  had finished by then (the same data H2b's own signal and exit vote see that day):
  1. c is eligible at that moment (H2b's four, or an extra coin picked at the latest screen date, section 5);
  2. c has the history H2b needs (399 finished daily candles);
  3. H2b's exit vote would **not** fire on today's data: fewer than 2 of the 14, 28 and 56-day returns are below zero.
  Whether c is already held is **not** part of the rule; an idea for a held coin is refused by `evaluate()` (no adding),
  for H2c and R2 alike. So the qualifying days depend on prices only, and are the same for every random run.
- **Matching the number of ideas.** In H2c's full-period run (the same run that gives the statistic E, default settings),
  count for each coin c: S_c = the qualifying coin-days on which H2c's own `signal()` said "enter", and Q_c = all
  qualifying coin-days. Every H2c signal day qualifies (2 of 3 returns strong means at most 1 below zero), so S_c <= Q_c.
  R2's chance is q_c = S_c / Q_c, so its expected number of ideas on c is exactly S_c. For each seed, the realised count
  varies around S_c; the report shows it. If S_c = 0, R2 never buys c.
- **Drawing.** For seed s, one `random.Random(s)` (Python's Mersenne Twister). At every decision moment, for every coin in
  the full candidate list (H2b's four plus every extra coin ever picked), in symbol order, one draw u = `rng.random()` is
  always taken; c gets a buy idea when it qualifies and u < q_c. Taking a draw even when c does not qualify keeps each
  seed's sequence the same whatever the eligibility. Seeds 100,000 to 101,999. Each idea then takes H2b's entry price, stop,
  take-profit and time stop and goes through `evaluate()`.

**Why this uses no future information.** The draws are independent of prices. Each qualifying test uses only candles
finished at the decision moment. The only whole-period quantity is q_c, a single number per coin. It tells R2 *how many*
ideas to make on each coin, never *which* days, and nothing about what happened after any day. So R2's entry days carry no
information about later returns. That is the null hypothesis: "entry days with no information".

**Why it does not systematically favour H2c's average.** Compared with R1, every change makes the benchmark harder to
beat, not easier (its *spread* is a separate matter, 9.1b):
- R2 decides at the same moment as H2c.
- It makes the same expected number of ideas per coin, so H2c gets no credit for trading the coins that did best more often.
- It never buys a coin that H2b's exit would sell on the same data, which removes R1's main tilt (paying round trips on
  downtrend buys).
- Exits, sizing and risk rules are identical.

**What remains unmatched (and could still tilt the result either way):**
- **Volatility at entry:** H2c skips days in its volatility pause; R2 can buy on them.
- **Strength of the trend at entry:** R2 buys any non-downtrend day; H2c only strong ones. This is the difference under
  test, not a bias.
- **Timing across the period:** H2c's signals bunch in rallies and in certain months; R2 spreads its ideas over all
  qualifying days of the period. The test therefore credits H2c both for *which months* and for *which days* it trades.
- **Bunching across coins:** H2c's signals often come on several coins at once, so the position limits refuse more of its
  ideas than R2's spread-out ones; the trades actually taken differ.
- **Holding time:** reported (mean days held, profit per day held), not matched.
- **The realised number of ideas** per seed: random around S_c, not equal.

#### 9.1b What R2's calibration and design mean statistically

**(a) S/Q is calibrated in sample.** S_c (H2c's signal days per coin) comes from the same research data on which H2c is
then judged. So R2 is a **conditional** randomization test: it asks whether, *given* that H2c entered coin c on S_c
days of this period, H2c's days were better than S_c days drawn at random from the qualifying ones. Consequences:

- **No outcome leaks into the benchmark.** S_c is a count only, fixed by H2c's deterministic rules; it says nothing about
  which days, or what any trade earned, and no choice by anyone goes into it. Conditioning on it does not tilt the
  benchmark toward or against H2c on average.
- **Counts are not independent of the market.** H2c signals more in strongly trending periods, so S_c partly reflects how
  the period went. The test holds that fixed and asks only about the *choice of days*; it says nothing about whether H2c
  would signal as often, or as profitably, elsewhere.
- **The p-value is relative to a reference set built from this data,** not an exact probability under a model of how
  prices are generated. It is a descriptive measure of how unusual H2c's days are among random qualifying days of the
  same period. It is in-sample throughout: the statistic E is in-sample too (full period, default settings).

**(b) Which constraints the null preserves, and which it does not.**

| Constraint | Preserved by R2? | Consequence |
| --- | --- | --- |
| Exits, sizing, costs, `evaluate()` and every risk rule, including the 3-position, 1-per-coin and 2-per-group limits | **Yes, as rules**: every R2 idea goes through the same engine | Fair |
| How often those limits actually bind | **No**: H2c's signals bunch, R2's are spread out, so the limits refuse more of H2c's ideas | The trades actually taken differ in number and timing |
| Correlated signals across coins (several coins signalling in the same rally) | **No**: R2 draws each coin independently | R2's trades are less correlated than H2c's |
| Clustering of entries in time and across market regimes | **Only partly**: no R2 buy on a coin whose exit vote would fire, so downtrend days are excluded as for H2c; within the remaining days R2 spreads its ideas evenly | R2 does not bunch in rallies as H2c does |
| Volatility pause, trend strength at entry, holding time | **No** (9.1a) | Part of what is under test, or reported only |

**The main statistical consequence:** because R2's trades are less bunched and less correlated than H2c's, the spread of
R2's E_s across seeds is likely **narrower** than the spread an uninformative strategy with H2c's bunching would have.
That makes the comparison **anti-conservative**: such a strategy would clear the hurdle more often than alpha. The same
holds for R1. So clearing the hurdle means only "H2c's days beat evenly spread random days by more than their spread";
it is **not** a calibrated probability, and not evidence of a statistically significant edge (9.1c).

**A check fixed now, reported with every p-value (information only; it does not change the gate):**
- the standard deviation of E_s over the 2,000 seeds, for R1 and R2;
- the standard error of H2c's own E: the bootstrap of 9.4 (active months, 10,000 draws, the same seed) applied to the
  full-period trades behind E;
- if H2c's standard error is more than **1.5 times** a benchmark's standard deviation, the report prints beside that
  p-value: **"benchmark spread likely too narrow for H2c's clustering: this hurdle is likely too easy"**;
- a cautious standardized gap, (E - mean of E_s) / max(standard deviation of E_s, H2c's standard error), shown beside it.

This check is **diagnostic only: it does not correct** the benchmarks' likely underestimate of variability, and no
adjusted or "corrected" p-value is computed from it. A gate pass with the warning is reported with the warning in its
outcome line, never without it.

**Considered and not adopted:** a benchmark that keeps H2c's bunching by shifting its whole signal calendar in time
(all coins together) by a random number of days. Not adopted because the shift wraps rising and falling years together,
adjacent shifts are nearly identical (few independent draws), and it adds a third test. Recorded in section 12.

**(c) Fixed before any result, and no further runs.**
- Fixed at the approving commit: the statistic E, its period, mode, costs and default settings; R1's chance 1/30; R2's
  rules and the formula q_c = S_c / Q_c; the generator, the seeds (0 to 1,999 and 100,000 to 101,999) and N = 2,000; the
  p-value formula; m = 7 and alpha; the hurdle bands (clears, fails, unclear); the dispersion check; the labels and the
  words allowed in reports (9.1c).
- S_c and Q_c are computed mechanically by the reviewed stage-B code from H2c's full-period run, in the same stage-D run
  that produces the p-values; nobody sets or adjusts them.
- Stage D runs **once**. After any result is seen: no more seeds, no other seeds, no change to N, the statistic, the
  settings, the benchmarks, q_c, alpha or the bands, and no additional benchmark. Any of these would be a new exploratory
  experiment with its own document, counted in m.
- A technical failure (a crash, a full disk) before any result is shown may be re-run from scratch with identical code and
  inputs; it is logged with its cause. The stage-B code's SHA-256 is recorded with the results, so the run that produced
  them can be identified.

#### 9.1c The role of R1 and R2: approach A (chosen 26 Oct 2026, before any result)

Sultan asked for one of two approaches: (A) keep R1 and R2 as exploratory diagnostics without any claim of a significant
edge, or (B) design a better-calibrated benchmark that keeps temporal clustering and cross-coin dependence. **This
protocol uses A.** Reasons, all about design and none about what result H2c might get (no result exists):
- every calibrated alternative considered (shifting H2c's whole signal calendar in time; resampling it in blocks;
  permuting entry days within month-and-regime groups) brings its own assumptions (how long the blocks are, whether
  behaviour is stable within them) and would itself need validating by simulation on prices with a known zero edge
  before its result could be trusted;
- it would add code, many more runs on the 512 MB server, and one more fork;
- the gate's decision does not rest on the random comparison: rules 2 to 6 use out-of-sample results with block
  intervals (9.4).
B can still be proposed later as its own exploratory experiment, reviewed separately and counted in m.

**What R1 and R2 can support:**
- a **hurdle with an asymmetric meaning.** Because the benchmarks probably understate variability, **failing** to clear
  it is informative: H2c's days did not even beat evenly spread random days, so the gate fails. **Clearing** it is weak:
  it is necessary, not evidence;
- a **description**: how H2c's profit per trade compares with random entries under the same exits, coins and risk rules
  (and, for R2, the same decision time, idea rate and avoidance of downtrend coins), shown as the share of random runs
  that did as well (c / N, and p as defined in 9.3), with the dispersion check of 9.1b beside it.

**What they cannot support:**
- that H2c has a **statistically significant** or **calibrated** entry edge, whatever c or p is;
- that H2c is better than H2b, or that the extra coins add value;
- that any edge holds out of sample, in future markets, or in live trading;
- anything about total return, drawdown or risk-adjusted return;
- an independent confirmation of any kind.

**Words in reports:** "clears the hurdle", "fails the hurdle", "unclear"; never "significant", "statistically proven",
"demonstrated edge" or similar for R1 or R2.

### 9.2 Is the comparison fair? What it can and cannot show

| Factor | R1 (fixed chance) | R2 (matched) | Effect on the comparison |
| --- | --- | --- | --- |
| How often ideas come | About 0.2 per coin per day, far more than H2c; most are refused by the position limits, so the trades R1 takes are the ideas that arrive just after a slot frees up (after exits, which bunch in sell-offs) | Same expected number of ideas per coin as H2c | R1's trades are not a random sample of days; R2 removes most of this |
| Decision time | Any 4h close | H2c's daily decision time | R2 matches |
| Exits | H2b's, including the momentum vote | Same | Random buys in a downtrend under R1 are usually sold at the next daily check, paying a full round trip of costs (fees 0.2%, spread 0.02%, slippage 0.05% plus 0.5-1.0% on the sale: about 0.8-1.3% of the trade) for little or no move. That lowers R1's profit per trade and **favours H2c**. R2 only buys when the vote would not fire, which removes most of it |
| Holding time | Short on average (quick exits) | Closer to H2c's, not equal | In the mostly rising 2020-2025 market, longer holds collect more upward drift per trade. Neither benchmark matches holding time exactly; mean holding time and profit per day held are reported for H2c, R1 and R2 (information only) |
| Market exposure | Much more time in the market | Similar order to H2c | Does not affect profit *per trade*; total P&L, drawdown and Sharpe are **not** compared with random runs |
| Coins, sizing, risk rules | Same as H2c | Same as H2c | Fair |

**What clearing the hurdle on both describes:** on the reused research period, H2c's entries made more net profit per
trade than nearly all random entries with the same exits, coins and risk rules (and, for R2, the same decision time,
rate and avoidance of coins H2b's exit would sell). Because the benchmarks' spread is probably too narrow (9.1b), this
is a description and a necessary hurdle, **not** evidence of a significant edge (9.1c).

**What it cannot show:** that H2c is better than H2b; that the extra coins add value (both benchmarks trade the same coins;
that is section 10, rule 4); that the edge holds out of sample (the statistic is in-sample, full period, default settings);
anything about total return, drawdown or risk-adjusted return; anything about future prices or live trading at 100 USDT; or
an independent confirmation of any kind.

### 9.3 The test statistic, multiple testing and Monte Carlo uncertainty

- **Null hypothesis (each benchmark):** H2c's entry choice has no information; its profit per trade is a draw from that
  benchmark's distribution.
- **Statistic:** E = mean net P&L per closed trade over the full research period (2020-01-01 to 2025-09-30), edge mode,
  normal costs, H2c's default settings (`threshold` 0.5, `atr_mult` 3), as check 7 of `bot.backtest.selection` computes
  today; E_s for each random seed s. N = 2,000 seeds per benchmark, all run.
- **p-value:** p = (1 + #{s : E_s >= E}) / (N + 1), for R1 and R2 separately.
- **The family, m = 7.** One member per evaluation of a strategy's entries on this research period that ended in a keep or
  reject decision: H1 (BTC, ETH); H2 (BTC, ETH); H2 (4 coins); H3 (BTC, ETH); H2b (4 coins); H4 (BTC, ETH, 1h); H2c.
  Not counted, and why:
  - the settings grid inside each walk-forward (a choice inside one run; the random statistic uses default settings, so
    nothing is chosen, and the walk-forward's own results are out of sample by construction);
  - the 20% sensitivity runs (they can only reject);
  - buy-and-hold, cash and random runs (benchmarks, not candidates);
  - H2's holdout run (different data, spent);
  - I1-I3 (never run);
  - R1 and R2 (two required tests of one candidate: H2c must pass both, so the chance of a false pass cannot exceed
    either test's level).
- **Earlier selection and informal experiments, accounted for in words, not numbers:** the original coin screen (it picked
  XRP and BNB with the same data; a selection step that H2, H2b and H2c all inherit); C1 and C2 (considered, not run); the
  random-walk calculations in earlier messages (a model, no market data); `bot signals` and `bot entries` on forward data
  (not research data); and the choices in section 12. A Bonferroni count cannot include ideas that were never run, so the
  true false-pass risk is **higher than 5% by an unknown amount**.
- **What the threshold controls.** Bonferroni at 0.05 / 7 bounds the chance of at least one false pass at 5% **only
  for the seven listed tests, and only if each test's p-value is valid under its null**, that is, if the random
  benchmark is a correct model of "entries with no information" (9.1b explains why R1 and R2 probably make the p-values
  too small for H2c's bunched entries) and the tests and their reporting were fixed in
  advance (this holds for any dependence between the seven). It does **not** control the informal and historical
  choices above, the six earlier runs (tested at 5% each), or any choice outside the family. **No claim is made that
  the overall false-positive probability of this research is 5% or less**; it is higher by an unknown amount.
- **The hurdle level:** alpha = 0.05 / 7 = **0.00714** for each of R1 and R2, used as a pre-set hurdle (9.1c), not as a
  calibrated significance level. The six earlier runs were tested at 5% each, so the
  family as a whole never had a 5% family-wise error rate; m = 7 sets H2c's level as if all seven had been tested at
  alpha. Earlier verdicts are not revisited (H2b's plan is frozen). For information, Phase 1 also reports H2b's own p-values
  under R1 and R2 with this method.
- **Every further run on this research period** (including any new exploratory experiment) adds 1 to m; the new alpha
  = 0.05 / m is recomputed and printed with every result.
- **Monte Carlo uncertainty:** c = #{s : E_s >= E}, with a 95% Clopper-Pearson interval for the true p. **Clears the
  hurdle** only if the interval's upper end is below alpha (**c <= 6 of 2,000**); **fails the hurdle** if its lower end
  is above alpha (**c >= 23**); **unclear** for c = 7 to 22, treated as not clearing it. N is not increased after a
  result is seen.
- **Is N = 2,000 adequate** for placing the hurdle? Clearing it happens with probability 0.93 if the true p is a quarter of alpha, 0.43 at
  half, and 0.01 at alpha itself: it rarely passes by Monte Carlo luck, but can miss a modest real effect. N = 1,000 would
  need c <= 1 and is not adequate. Runtime is measured on 10 seeds first; if 2 x 2,000 runs do not fit the server, the test
  is reported **not run** and the gate cannot pass.
- **Never independent confirmation:** whatever its p-value, no result on this reused data is described as an independent
  confirmation.

### 9.4 Intervals: block bootstrap, and what happens with few months

- **Blocks:** the **active months** of the set measured (months in which at least one of its out-of-sample trades was
  opened). For the paired H2c minus H2b difference: months active in either.
- **Resampling:** 10,000 draws, each of k blocks with replacement, where k is the number of active months;
  `random.Random(20261026)`. Per draw, expectancy = sum of net P&L / number of trades in the drawn blocks; for the
  difference, the sum of H2c's net P&L minus H2b's over the same drawn months. The 90% interval is the 5th to 95th
  percentile.
- **Block length:** the same with quarter-year blocks. **The gate uses the lower of the two lower ends** (the more cautious).
- **Minimum evidence:** an interval is used by the gate only if its set has **at least 20 trades, 15 clusters and 12 active
  months**. Below that, it is printed with "too few blocks" and the gate rule it feeds is **insufficient evidence**.
- **Always reported:** trades, clusters, active months, trades per month, and the largest share of net P&L from one month
  and from one cluster.
- **Regimes:** per regime, trades, clusters and active months. A regime with fewer than 10 trades or 5 clusters is "too few
  to judge".
- **Known weakness:** blocks of a month or a quarter do not capture dependence that lasts longer, as regimes do; the
  intervals can still be too narrow (section 16).

## 10. The Phase 1 gate (all must hold; fixed now)

1. **Selection rule:** the 7 checks of `bot.backtest.selection`, unchanged, except check 7 (random entries), which is
   replaced by **clearing the random-entry hurdle on both R1 and R2** (9.3). This is a necessary condition only; it is
   never reported as evidence of a significant edge (9.1c).
2. **Net expectancy:** H2c's out-of-sample expectancy after doubled costs is positive and at least 75% of H2b's.
3. **Drawdown:** H2c's worst test window, as deployed, at most H2b's plus 1 percentage point, and below 6%.
4. **Primary outcome (the extra coins):** the extra-coin trades meet the minimum evidence of 9.4, their out-of-sample
   expectancy after doubled costs is positive, and H2c minus H2b (doubled costs) is positive. Each with its cautious 90%
   interval (9.4).
5. **Robustness:** rules 2 and 4 hold in both halves of the out-of-sample period (2022-01 to 2023-10, 2023-11 to 2025-09);
   **at least two regimes** can be judged (9.4) and rules 2 and 4 hold in each regime judged; H2c stays positive without
   its best coin, its best cluster and its best month.
6. **Research target, not evidence:** at least 25% more completed out-of-sample trades than H2b. Missing it means H2c does
   not do what it was built for. Meeting it proves nothing; rules 1 to 5 decide.

**Outcomes (exactly one is reported, with every number behind it):**

| Outcome | When | Meaning |
| --- | --- | --- |
| Stopped | A stage-B data check stopped the study | Nothing can be concluded |
| Fails | Any rule fails | The coin change does not deserve further investigation in this form |
| Insufficient evidence | No rule fails, but a minimum of 9.4 is not met, fewer than two regimes can be judged, or a random-entry hurdle is unclear | The data cannot answer the question; nothing is relaxed to get an answer |
| Passes the exploratory gate, effect not demonstrated | All rules hold; a cautious lower end in rule 4 is at or below zero | Deserves further investigation; the effect may be zero |
| Passes the exploratory gate, effect shown on reused data | All rules hold; both rule 4 lower ends above zero | Deserves further investigation; still exploratory, not a confirmation, not "better than H2b" |

In every case the result is reported in full and recorded in `RESEARCH`; H2b continues unchanged, and nothing further
starts without Sultan's separate decision.

### 10.1 What passing means, and does not

Passing the gate, in either form, means only that **H2c may deserve further investigation**. It does **not**
establish that H2c is better than H2b, that it will be profitable in future markets, that its edge is real rather than
a product of reused data and earlier choices, or that it is suitable for live trading. No report, message or summary
may describe a pass in any of those terms, or describe clearing the random-entry hurdle as a statistically
significant or calibrated entry edge. Any next step (for example a forward test with simulated money) needs its
own plan and Sultan's explicit approval; live trading is outside this research entirely.

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
| Random-entry hurdle at the 99.29th percentile (m = 7), N = 2,000, unclear band | Set from the 6 earlier runs |
| Splitting listings at 3-day gaps | The archive check of 26 Oct (LUNAUSDT, FTTUSDT), done before any price was seen |
| A second, matched random benchmark (R2), and its exact rules | Analysis of the existing benchmark's design (revisions 4 and 5); no result seen |
| R2 calibrated to H2c's own signal counts on the same data (in sample) | A design choice to match frequency; declared in 9.1b |
| The dispersion check and its 1.5x reporting threshold | Revision 6; a reporting rule chosen before any result; it does not change the gate |
| A calendar-shift benchmark considered and not adopted | Revision 6 (reasons in 9.1b) |
| Approach A (R1 and R2 as diagnostics and a hurdle) instead of B (a calibrated benchmark) | Sultan's review of revision 6; chosen on design grounds before any result (9.1c) |
| Minimum-evidence rules (active months, clusters, two regimes) | Sultan's review of revision 3; no result seen |

Every Phase 1 result is therefore **exploratory**, not an independent confirmation.

## 13. Work needed at stage B (after Sultan approves the code; one small module each, with tests)

1. Archive loader with checksums, unit detection, gap splitting, and the DQ-1 to DQ-8 report.
2. The point-in-time screen (section 5), one write, never re-run.
3. Walk-forward with a universe per moment; H2b and H2c on identical data.
4. Phase 1 report: the definitions of section 7, clusters, the bootstrap of 9.4, both random benchmarks of 9.1 with m, alpha,
   c and its interval, and the gate of section 10 with the labels of section 8.

Results come only from runs on the server.

## 14. Limitations

Reused research data (6 earlier runs); post-hoc choices (section 12); survivorship bias removed only as far as DQ-3
confirms; ranking and worst-fall rules can tilt toward past winners; today's or last known exchange filters used for
the past; correlated coins (fewer independent trades than the count); a mostly rising 2020-2025 market; modelled costs
(cautious, but real fills differ, and smaller coins may have wider spreads, which the risk engine refuses above 0.10%);
the 512 MB server limits coins and random runs.

## 15. Sultan's decisions (stage A)

- [ ] Approve and freeze this protocol text (revision 7: sections 1 to 14 and 16). This approves **no**
      implementation, download, screen, coin selection, backtest, forward test, service or live trading.
- [ ] Separately, later: approve the stage-B code; then the stage-B run; then stage C; then stage D.

## 16. Unresolved limitations (known before any result)

1. **Reused data.** Six runs and many design choices have used the research period; any Phase 1 result is exploratory
   and can never be an independent confirmation.
2. **Uncountable forks.** The multiple-testing bar counts runs, not the ideas considered and dropped, the screen change,
   or the choices in earlier plans; the true false-pass risk is higher than 5% by an unknown amount.
3. **The random test is in-sample and per trade.** It uses the full research period at default settings, not the
   walk-forward, and compares profit per trade only: not total return, drawdown, Sharpe, or the extra coins' value.
4. **Benchmark mismatch remains partly.** R2 matches decision time, frequency and "not in a downtrend", but not the
   holding time of H2c's trades or the exact market conditions of its signal days.
5. **Low power.** The extra coins may produce few trades; the likely honest outcome is "insufficient evidence" or "not
   demonstrated", and the design accepts that rather than lowering the bar.
6. **Few independent blocks.** Months and regimes are few (about 45 out-of-sample months, mostly rising markets);
   monthly blocks miss dependence that lasts longer than a month, so intervals may still be too narrow.
7. **Survivorship and data.** Removed only as far as DQ-3 confirms; delisted coins use their last known exchange
   filters; positions in a listing that stops are counted as total losses (cautious, not exact).
8. **Selection tilt.** The worst-fall rule and the volume ranking use past data only but can favour past winners.
9. **H2b's own coins** (XRP, BNB) keep the original screen's look-ahead, so the baseline is not clean either.
10. **Not tested here at all:** the forward behaviour of H2c, live fills, the 100 USDT minimum-order limit, and any
    period after 2025-09-30.
11. **R2 is calibrated in sample.** It is a conditional test given H2c's own signal counts on this data; its p-value is
    relative to random days of the same period, not a probability about how prices are generated.
12. **The random benchmarks do not keep H2c's bunching** (correlated coins, rallies, how often limits bind). Their spread
    is likely too narrow, so clearing them is weak (anti-conservative); the 9.1b check shows how much, but does not
    correct it. Under approach A they are diagnostics and a hurdle only; this protocol has **no calibrated test of an
    entry edge**.

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
