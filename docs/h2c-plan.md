# H2c research plan: do more coins give H2b more good trades?

Status: **DRAFT, revision 2, for Sultan's review (26 Oct 2026). Not approved. Nothing is built.**
Sultan approved further research *planning* only (26 Oct 2026): not H2c's implementation, not a new service,
not live use. Once a version of this plan is approved, its commit is the record of what was decided before any
H2c result existed; changing anything after that, including a rule, a coin, a date or a pass rule, makes a new plan.

Revision 2 (after Sultan's review of revision 1): H2c is now only candidate C3 (more coins, H2b's rules
unchanged); C1 and C2 are deferred and not run. The coin screen is point-in-time and rolling; the listing
cutoff is explained and marked as chosen after seeing the first screen; the 25% trade target is a research
target only; the random-entry gate is calibrated to the number of strategies tested; the limits of 30 trades
are spelled out.

This plan changes nothing about H2b, its forward test (`~/forward-h2b`), its settings, `config/default.yaml`,
the risk limits, the live account, its capital (100 USDT) or any safeguard. No live trading, no real orders,
no new service, and no implementation is merged until Sultan approves it.

## 0. Why this plan, and what does not justify it

`bot entries` (run on the server on 26 Oct 2026): H2b gave **no entry signal** on any coin since the accounts
started, 16 of 16 coin-days on the paper account (23-26 Oct): "0 of 3 lookbacks strong" on 13 days, "1 of 3"
on 3; the volatility pause never decided anything; nothing was rejected, unfilled, blocked or missed.

That is **not evidence that H2b is too strict**: 4 days, 4 coins that move together, in a falling market where a
long-only momentum strategy is meant to stay out. This plan asks a general question and must not be judged by
whether H2c would have traded in those 4 days. H2b's forward days are not used to design or choose H2c; the
`bot entries` summary above is the only fact about them used here, declared so it cannot leak in unnoticed.

## 1. The baseline: H2b, unchanged (`docs/h2b-plan.md`, frozen)

- Daily candles from 4h candles; one decision a day at the first 4h close after 00:00 UTC; finished candles only.
- `vol` = 30-day volatility of daily closes. For L in 14, 28, 56 days: `r_L` = close / close L days ago - 1;
  **strong** when `r_L` > `threshold` x `vol` x sqrt(L).
- **Entry:** at least 2 of 3 lookbacks strong, and today's `vol` below the 80th percentile of the coin's 30-day
  vol over the 365 days before (fewer than 365 values: no entry).
- Limit buy 0.10% above the last close; stop `atr_mult` x daily ATR(14) below; take-profit 3x the stop distance;
  time stop 30 days; exit at market when at least 2 of the 3 returns are below zero.
- Tunable: `threshold` and `atr_mult`, chosen by the walk-forward rule (forward test: 0.60 and 3).
- Coins BTC, ETH (group "majors"), XRP, BNB (own groups). Every entry through `evaluate()`.
- Results (real server runs): walk-forward 104 out-of-sample trades, +1.90 USDT per trade with doubled costs.
  Forward test: day 3 of at least 365, 0 of at least 30 closed trades: not evidence yet.
- **Declared weakness of the baseline itself:** XRP and BNB were picked by the screen using 2022-10 to 2025-10
  data and then traded in a walk-forward that covers the same years, so H2b's own coin choice has look-ahead.
  H2c's design below avoids that for the added coins and reports their effect separately.

## 2. Research question and hypothesis

**Question.** If H2b's rules, settings, risk limits and correlation controls stay exactly as they are, and only the
set of eligible coins grows by up to 2, chosen without hindsight, are there more trades whose net expectancy after
costs is positive, without more drawdown or a dependence on one coin, cluster, month or regime?

- **Null (H0):** the added coins add trades but no expectancy: their trades' net expectancy after doubled costs
  is zero or below, because their trends are mostly the same market move as BTC's.
- **Alternative (H1):** the added coins' trades have positive net expectancy after doubled costs, and H2c's
  drawdown is not materially worse than H2b's on the same prices.

## 3. H2c, exactly

**Strategy rules:** H2b's, character for character (entry, pause, stop, take-profit, time stop, exit, decision
time, the two tunable settings and their grid). H2c differs from H2b only in its coin list. H2b's files
(`h2b_blend.py`, `strategies/base.py`) are not changed; H2c reuses them unchanged.

**Coins:** H2b's four (BTC, ETH, XRP, BNB) always, **plus up to 2 extra coins** picked by the screen below.
The total of extra coins stays within Sultan's memory cap (at most 4 beyond BTC and ETH).

**Risk and correlation controls, unchanged:** R-04 (at most 3 positions), R-05 (1.5% open risk), R-08 (at most
2 per correlation group), every other rule in `evaluate()`, the same config. Extra coins compete for the same
slots; they never raise exposure.

### 3.1 The screen: point-in-time and rolling (how the coins are chosen)

The screen is run at a **decision date T**, using only data before T. Its rules are the original screen's
(`bot.backtest.universe`, fixed before any coin was looked at), with two changes, both stated in 3.2:

1. **Candidate pool at T:** every Binance USDT spot pair that was trading at T, *including pairs delisted later*,
   taken from Binance's public historical data archive (data.binance.vision). Not from today's list.
   Excluded, as before: stablecoins, fiat, gold, wrapped, staked and leveraged tokens; H2b's four coins.
2. **Listing:** daily candles from at least **4 years before T** (a 3-year screen window plus 1 year of trading
   before it). (Original: back to 2019-10-01, a fixed date.)
3. **Screen window:** the 3 years before T, `[T - 3 years, T)`. Unchanged rules over that window:
   30-day vol above 2x BTC's on at most 25% of days; worst fall at most 70%; at least 20 million USDT traded on
   at least 95% of days.
4. **Pick:** up to 2 passing coins with the highest median daily volume in the window.
5. **Correlation group (R-08 as written):** correlation of daily returns with BTC over the window of 0.8 or more
   joins "majors" (with BTC and ETH); below that, the coin's own group.

**Where T is:**
- **Backtest (Phase 1):** T = the start of each walk-forward test window (6-month windows from Jan 2022 to
  Sep 2025). The coins traded in a window were chosen only with data before it, so no out-of-sample result uses
  hindsight. The coin list may differ between windows.
- **Forward test (Phase 2, only if approved):** T = **2025-10-01**, the last date before the spent holdout year.
  So the forward coins are chosen on 2022-10-01 to 2025-10-01 data and must have candles from 2021-10-01. In
  addition they must be trading today with OPO/OTO/OCO and STOP_LOSS offered; that is a fact about the present,
  not hindsight about the past. The screen cannot use the holdout year, so it is about a year old at the start.

**What the screen never uses:** returns, profits, or any strategy or backtest result. It sees only volatility
(relative to BTC), the worst fall, trading volume and listing date, and only before T.

**Residual biases, declared:** the "worst fall at most 70%" rule is a stability rule from the design package, but a
coin's worst fall is related to how it performed; and ranking by volume favours coins that became popular, often
after rising. Both use only data before T, so they are ex-ante filters, not hindsight; they can still tilt the
choice toward coins that did well before T.

### 3.2 The two screen changes and why (declared as chosen after seeing the first screen)

- **Point-in-time pool (fixes a bias).** The original screen took coins listed *today*, so delisted coins were
  missing (survivorship bias), and it chose coins with 2022-2025 data that the walk-forward then traded
  (look-ahead). The rolling point-in-time screen removes both for the added coins, as far as the data allows.
- **Listing cutoff from 2019-10-01 to "4 years before T" (= 2021-10-01 for the forward coins).** The original
  date was chosen so that every coin covered the whole research period from 2020 with one fixed coin list. A
  rolling screen does not need that, so the requirement is tied to the screen date instead: the full 3-year
  window must exist, plus one year of trading before it, so no coin is judged on its first, unusual year after
  listing. For T = 2025-10-01 this gives 2021-10-01.
- **This change was chosen after seeing that the first screen found only 2 of 4 possible coins** (XRP, BNB). The
  rationale was written afterwards. It is therefore exploratory, not an independent confirmation, and any Phase 1
  result for H2c is labelled so.
- If fewer than 1 extra coin passes for the forward date, H2c ends there. No rule is changed again to admit more.

### 3.3 Which coins? Not known yet, on purpose

The exact coins are **not identified in this plan**. This container cannot reach Binance, and naming likely coins
from memory would anchor the choice. The order of work makes hindsight impossible:

1. After approval, the screen (code only, no strategy) runs **once** on the server for every T.
2. Its result (coins, groups, the numbers behind each pass or fail, the delisted pairs it saw) is written once,
   never re-run, and **added to this plan as an appendix for Sultan's review before any strategy backtest runs
   on those coins.**
3. Only then Phase 1b runs H2b's rules on them.

If the archive turns out not to hold delisted pairs, the report says so, labels C3's results "survivorship bias
not removed", and Sultan decides whether to continue.

## 4. Data rules

- **Phase 1 uses only the research period, 2020-01-01 to 2025-09-30** (warm-up from 2019-01-01). It has been
  used for H1, H2, H3, H2b and H4, so every Phase 1 result is labelled **"previously used data: exploratory,
  not evidence"**. Passing proves little; failing ends H2c.
- **The spent holdout year (2025-10-01 to 2026-10-01) is never read, loaded or run** for H2c: not for the screen,
  design, settings or checks. In a forward test, its candles may serve only as indicator history before H2c's
  start, by the same rule as H2b's forward report (`history.load_with_forward`).
- **2026-10-01 to H2c's start** is not used for design or choice either. **No forward-test result** of H2b or H2c
  is examined for H2c's design.

## 5. Phase 1: exploratory analysis on previously used data

**1a. The screen** (section 3.3): once, recorded, reviewed by Sultan before 1b.

**1b. The backtest.** H2b and H2c on the same walk-forward windows, the same engine, simulator, fees, slippage,
risk config and settings grid, as deployed (6% breaker halts). H2b here is the frozen baseline re-run with its
own four coins; H2c adds the window's screened coins.

**Measured** for H2b, H2c, and the extra coins' trades alone, per coin, per correlation group and per regime
(`bot.analysis.regime`: bull, bear, sideways; high volatility separately):
signal days; entries; completed trades; gross P&L; fees paid; slippage paid; net P&L; expectancy per trade in USDT
and in units of planned risk, at normal and doubled costs; maximum drawdown (worst window, as deployed); Sharpe
and Sortino of daily equity changes; return / maximum drawdown; time in market; and a **90% block-bootstrap
interval** (by week, 10,000 resamples) for every expectancy and for the H2c minus H2b difference.

**Correlated trades.** Trades in the same correlation group that overlap in time count as one *cluster*. Every
table shows trades and clusters; the bootstrap resamples whole weeks so overlapping trades stay together.

**Number of strategies tested on the research period.** H1; H2 on BTC and ETH; H2 on four coins; H3; H2b; H4:
**6 runs** so far. H2c is the **7th**. C1 and C2 from revision 1 are **deferred and not run**; if they ever are,
they count, and the bar in 5.1 is recomputed for them.

### 5.1 The random-entry gate: what it means

- **Null hypothesis:** H2c's entry timing carries no information. Its out-of-sample expectancy per trade after
  doubled costs is a draw from the distribution of random entries on the same coins and windows, entered at the
  same average rate, with exactly the same stops, take-profits, time stops, exits and risk rules
  (`RandomEntry.with_exits_of`).
- **The test:** H2c passes if its expectancy is above the random distribution's **(1 - 0.05 / m)** percentile,
  where m = the number of strategy runs tested on this data, H2c included. With m = 7 that is the
  **99.29th percentile**: each test at 0.71%, so the chance that any of the 7 passes by luck alone is at most
  about 5% (Bonferroni).
- **How well it is calibrated:**
  - It is conservative where the 7 strategies are alike: they share data and ideas, so their results are
    correlated, and Bonferroni over-corrects.
  - It is **not** conservative about informal choices it cannot count: the screen change in 3.2, the decision to
    test C3 first, and choices made in earlier plans. The true number of forks is larger than 7. That is why
    Phase 1 can rule H2c out but cannot confirm it.
  - It tests entry timing, not the coin addition. For the added coins, the separate check in 5.2, rule 4,
    applies.
- **Enough random runs:** the existing default is 200 runs, too few to place a 99.29th percentile (about 1 run
  above it). H2c's check uses **2,000 runs** (about 14 above it). If 2,000 do not fit the server's time or memory
  (measured first), 1,000 are used, and the report shows the binomial uncertainty of the percentile.

### 5.2 The Phase 1 gate (all must hold; fixed now)

1. All 7 checks of the selection rule (`bot.backtest.selection`), unchanged, except the random bar of 5.1.
2. **Net expectancy:** H2c's expectancy per trade after doubled costs is positive and at least 75% of H2b's on the
   same windows.
3. **Drawdown:** H2c's worst window, as deployed, at most H2b's plus 1 percentage point, and below 6%.
4. **The added coins:** their own trades have positive net expectancy after doubled costs, and H2c's total net
   P&L minus H2b's is positive. Both are shown with their 90% intervals; if an interval's lower end is below zero,
   the report says **"not demonstrated"** next to it.
5. **Robustness:** rules 2 and 4 hold in both halves of the walk-forward period, and in every regime with at least
   10 trades; H2c stays positive without its best coin, best cluster and best month.
6. **Research target, not evidence:** at least **25% more** completed out-of-sample trades than H2b. Missing it
   means H2c does not do what it was built for, so it stops. Meeting it proves nothing by itself; rules 1 to 5
   decide.

If H2c fails any rule, it is rejected and recorded in `RESEARCH`; H2b continues unchanged. Passing makes H2c
eligible for Phase 2 only **if Sultan approves Phase 2 then**.

## 6. Phase 2: prospective comparison on new prices (simulated money; only after the gate and approval)

- **Start:** the day Sultan starts it. Coins, groups and settings fixed and fingerprinted at the start; any
  change restarts H2c's test from zero.
- **Account:** its own folder (`~/forward-h2c`), 10,000 simulated USDT, the same simulator, fees, slippage, risk
  engine and config. It never places orders and never touches H2b's account or its forward test.
- **Same data for both:** both runners read the same live Binance prices; the verdict comes from replaying H2b
  and H2c on **one set of recorded candles** for the comparison window, as H2b's forward report already does.
- **Comparison window:** from H2c's start. H2b's earlier days count for H2b's own test, not here.
- **Length:** at least 12 months and at least 30 closed H2c trades, whichever is later, at most 24 months.
  Outages as in H2b's plan.
- **Early stop: only for failure** (the 6% breaker trips, or a risk-rule violation). No early stop for success.
- **Memory:** starts only after a measured check shows room for a second forward runner on the 512 MB server
  (49% memory and 33% swap in use on 26 Oct).

**Acceptance (all, judged once at the end; fixed now):** H2b's six forward checks applied to H2c (positive
expectancy after doubled costs; breaker never trips; at least 30 closed trades; positive without the best coin;
positive without the best month; beats random entries with the same exits on the same prices, here at the 99th
percentile); and on the same window, H2c's maximum drawdown at most H2b's plus 1 percentage point; positive without
its best cluster; the added coins' trades with positive net expectancy, shown with a 90% interval and "not
demonstrated" when its lower end is below zero. More trades than H2b is reported, as a target only.

**What a pass means:** H2c is a candidate, nothing more. It does **not** mean H2c is better than H2b (section 7).
Any further step needs Sultan's decision; live use would face the same minimum-order limit, and no capital or risk
change follows.

## 7. What 30 trades can and cannot show

- Planned risk per trade on 10,000 USDT at 0.25% is 25 USDT. H2b's walk-forward expectancy after doubled costs,
  +1.90 USDT, is about **0.08 of the planned risk**.
- One trade's result ranges from about -1x the planned risk (stopped out) to +3x (take-profit). Assume a standard
  deviation of about 1.5x the planned risk, about 37 USDT (an assumption; the real figure from the walk-forward is
  reported in Phase 1).
- With 30 trades, the standard error of the mean is about 37 / sqrt(30), about **6.8 USDT**: a 95% interval of
  about **plus or minus 13 USDT** around an expectancy near +2. **30 trades cannot tell a small edge from none.**
  The 30-trade minimum only means "enough to see whether it is clearly broken".
- Showing that the extra trades have positive expectancy of 0.2x the planned risk (5% one-sided, 80% power) needs
  about **350 trades**; showing H2c beats H2b by that margin needs about **880 trades per strategy**. At H2b's
  walk-forward rate (about 2 trades a month) plus maybe 1 from the extra coins, neither is reachable in 24 months.
- Therefore no report may say H2c is **better** than H2b unless its interval for the difference lies above zero.
  The expected honest outcomes are "not worse, works on its own" or "not demonstrated".

## 8. What stays exactly as it is

H2b (code, settings, coins, forward test, account); `config/default.yaml`; `CEILINGS` and `FLOORS`; `evaluate()`
and `_verify()`; order validation and protected entries; reconciliation and unknown-order handling; balance checks
and accounting; data-freshness checks; key-permission checks; the live switch; the live capital (100 USDT) and its
ceiling; risk per trade (0.25%); position, exposure, loss and drawdown limits; liquidity and correlation rules.

At 100 USDT the live account can only take stops up to about 3-4% (`bot entries`, live rules), so more signals
would mostly be refused there for the minimum order. That is a separate decision for Sultan.

## 9. Limitations

- **Reused data:** the research period has served 6 runs; Phase 1 can only rule H2c out.
- **A post-hoc choice:** the listing-cutoff change was made knowing the first screen found 2 coins.
- **Survivorship:** removed for the added coins only if the archive holds delisted pairs; otherwise it remains and
  is labelled. H2b's own coins (XRP, BNB) keep the original screen's look-ahead.
- **Residual tilt:** the drawdown rule and the volume ranking use only past data but can favour coins that rose.
- **A stale forward universe:** the forward coins are chosen with data ending 2025-10-01 (the holdout year cannot
  be used); the risk engine's live volume and spread checks still apply to every entry.
- **Correlation:** most coins move with BTC; the effective number of independent trades is lower than the count,
  and R-08 limits how many extra trades can happen at once.
- **Regimes:** 2020-2025 was mostly rising; a falling forward period gives few trades for H2b and H2c alike.
- **Costs are modelled:** the simulator charges the stop-slippage allowance on every market sale (cautious);
  real fills may differ, and smaller coins may have wider spreads (the risk engine refuses spreads above 0.10%).
- **Server:** 512 MB limits coins, random runs and runners.

## 10. Work needed after approval of implementation (one small module each, with tests)

1. The point-in-time screen (archive pool, rolling T, listing rule, one write, never re-run); its appendix for
   Sultan's review.
2. A walk-forward mode where each window has its own coin list; H2b and H2c on the same windows.
3. The Phase 1 report: extra coins separately, clusters, block bootstrap, m and the 99.29th-percentile bar with
   2,000 random runs, the gate.
4. Only if H2c passes the gate and Sultan approves Phase 2: its forward-test mode and end report.

Phase 1 runs on the server; results come only from those runs.

## 11. Sultan's decisions

- [ ] Approve H2c as only candidate C3 (H2b's rules, up to 2 extra coins), with C1 and C2 deferred.
- [ ] Approve the point-in-time rolling screen and the listing rule "4 years before T" (2021-10-01 for the forward
      coins), knowing it was chosen after the first screen's result.
- [ ] Approve the Phase 1 gate (section 5), including the 99.29th-percentile bar (m = 7) and 2,000 random runs.
- [ ] Approve the Phase 2 design and acceptance (section 6), to be used only if H2c passes the gate and only when
      Sultan starts it.
- [ ] Approve implementation of section 10, items 1 to 3 (separately from this plan's approval).
