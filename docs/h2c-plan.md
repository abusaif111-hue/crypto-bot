# H2c research plan: can a different entry design find more good trades than H2b?

Status: **DRAFT for Sultan's review (26 Oct 2026). Not approved. Nothing is built.**
Once approved, the approving commit is the record of what was decided before any H2c result existed.
Changing anything below after that, including a rule, a coin, a date or a pass rule, makes it a new plan.

This plan changes nothing about H2b, its forward test (`~/forward-h2b`), its settings, the risk
config, the live account, its capital or any safeguard.

## 0. Why this plan, and what does not justify it

`bot entries` (run on the server on 26 Oct 2026) found that H2b gave **no entry signal** on any of its 4 coins
since the accounts started: 16 of 16 coin-days on the paper account (23-26 Oct), 100% "no qualifying signal".
H2b's own reasons: "0 of 3 lookbacks strong" on 13 days, "1 of 3" on 3; the volatility pause was never the
deciding reason. No signal was rejected by the risk engine, unfilled, blocked or missed.

**This is not evidence that H2b is too strict.** It is 4 days of 4 correlated coins in a falling market
(on 25 Oct all four were below their 50-day average), where a long-only momentum strategy is meant to stay out.
H2b's walk-forward made 104 trades over Jan 2022 to Sep 2025, in bursts during rises. A design that trades more
*now* would, by construction, buy weaker trends. So this plan tests a general question, not a reaction to a
quiet week, and it must not be judged by whether H2c would have traded in these 4 days.

The forward days H2b has run (from 23 Oct 2026) are not used to design or choose H2c. The only fact about them
used here is the `bot entries` summary above; it is declared so it cannot leak in unnoticed.

## 1. The baseline: H2b, unchanged (`docs/h2b-plan.md`, frozen)

- Data: 4h candles turned into daily candles; one decision a day at the first 4h close after 00:00 UTC;
  finished candles only.
- `vol` = 30-day volatility of daily closes. For L in 14, 28, 56 days: `r_L` = close / close L days ago - 1;
  **strong** when `r_L` > `threshold` x `vol` x sqrt(L). Forward-test settings: `threshold` 0.60, `atr_mult` 3.
- **Entry:** at least 2 of 3 lookbacks strong, AND today's `vol` below the 80th percentile of the coin's own
  30-day vol over the 365 days before (fewer than 365 values: no entry).
- Limit buy 0.10% above the last close; stop `atr_mult` x daily ATR(14) below; take-profit 3x the stop distance;
  time stop 30 days; exit at market when at least 2 of the 3 returns are below zero.
- Coins BTC, ETH (group "majors"), XRP, BNB (own groups), from the coin screen (`bot.backtest.universe`).
- Every entry through `evaluate()`; risk config `config/default.yaml` unchanged.
- Known results (real server runs): walk-forward 104 out-of-sample trades, +1.90 USDT per trade with doubled
  costs (23 Oct 2026). Forward test: day 3 of at least 365, 0 closed trades of at least 30. Not evidence yet.

Every comparison below is against H2b exactly as frozen, on the same periods and the same costs.

## 2. Research question and hypotheses

**Question.** Can one change to H2b's entry design produce materially more trades without lowering
expectancy after doubled costs, raising drawdown, or depending on one coin, month or market regime?

| Candidate | Null hypothesis (what we expect unless shown otherwise) | Alternative |
| --- | --- | --- |
| C1 | The three lookbacks carry one trend signal; a single score selects the same days and does no better | The score selects different, better days |
| C2 | The 80th-percentile pause removes trades that are worse than average (it protects) | The removed trades are no worse; a 90th-percentile pause adds trades without harm |
| C3 | Adding coins adds trades but not expectancy (the new coins' trends are the same market move) | Added coins add trades with positive expectancy after costs |

## 3. The three candidates, exactly

Each changes **one** thing in H2b. Everything not named is H2b's rule, unchanged, including the exits, the stop,
the take-profit, the time stop and the two tunable settings (`threshold`, `atr_mult`), which are chosen by the
same walk-forward rule from the same grid as H2b. No candidate adds a tunable setting.

### C1 (H2c-S): one trend score instead of the 2-of-3 vote

- `z_L` = `r_L` / (`vol` x sqrt(L)) for L = 14, 28, 56; score `S` = (z14 + z28 + z56) / 3.
- Entry: `S` > `threshold`, AND the same volatility pause. (The exit stays H2b's 2-of-3 vote.)
- What the arithmetic says before any data: the lookbacks overlap (the 28-day window contains the 14-day one,
  the 56-day contains both). On a random walk their scores correlate at 0.71 (14/28 and 28/56) and 0.50
  (14/56), and at a threshold of 0.60 the pass rate is **24.6% for the score against 25.3% for H2b's vote**
  (400,000 simulated draws; a model, not market data). So C1 tests whether the vote picks *better* days;
  it is not expected to pick *more* days.

### C2 (H2c-P): volatility pause at the 90th percentile

- Entry: at least 2 of 3 lookbacks strong, AND today's `vol` below the **90th** percentile (H2b: 80th) of the
  coin's 30-day vol over the 365 days before. Nothing else changes.
- With stable volatility, the 80th-percentile pause blocks about 20% of days by construction and the 90th
  about 10%, but the pause applies only on days the momentum test passes, so the extra trades are at most that
  share of H2b's. In the 4 days measured, the pause never decided anything.
- The reason for the pause (momentum works worse after volatility shocks; Moskowitz, Ooi and Pedersen, 2012;
  Daniel and Moskowitz, 2016) is the reason C2 needs evidence that the extra trades are not the bad ones.

### C3 (H2c-U): more coins, the same rules, one screen rule changed

- H2b's rules unchanged; the coins are BTC, ETH, XRP, BNB **plus up to 2 more** chosen by the existing screen
  (`bot.backtest.universe`) with **one** rule changed: the listing requirement moves from "daily candles back
  to 2019-10-01" to "daily candles back to **2021-10-01**" (one year of history before the screen window starts,
  which the 365-day pause needs).
- Unchanged: the screen window (2022-10-01 to 2025-10-01, never the holdout), the exclusions (stablecoins, fiat,
  gold, wrapped, staked, leveraged), the stability rules (30-day vol above 2x BTC's on at most 25% of days;
  worst fall at most 70%), the liquidity rule (20 million USDT on at least 95% of days), picking by median
  volume, and the memory cap Sultan set (at most 4 extra coins in total, so at most 2 new ones).
- Correlation groups by the same R-08 rule: correlation with BTC of 0.8 or more joins "majors" (with BTC and
  ETH); below that the coin gets its own group. R-04 (3 positions), R-05 (1.5% open risk) and R-08 (2 per group)
  still cap everything, so new coins compete for the same slots.
- The modified screen runs **once**; its result is written once (like `h4-coins.json`) and never re-run.
  If fewer than 2 new coins pass, C3 is dropped: no other rule is changed to make more coins pass.
- Why this rule and no other: the stability and liquidity rules are risk controls and stay; the listing date
  only decided how much backtest history a coin has. Changing it is still a choice made after seeing that the
  first screen found only 2 of 4 possible coins, and it is declared as such.

### Recommendation

- **C3 is the only candidate with a defensible case for a forward test**, *if* it clears Phase 1: it is the only
  one aimed at more opportunities that changes no filter and no risk rule.
- **C1 and C2 are not recommended for a forward test now.** C1 is not expected to add trades (section 3, C1).
  C2 would not have changed any decision in the days measured, and the published evidence favours keeping a
  volatility pause. Both are still measured in Phase 1, because the question about them is worth answering on
  research data, and they count toward the number of variants tested.
- Only one H2c, at most, goes forward (section 6). If none clears Phase 1, there is no H2c and H2b continues.

## 4. Data rules

- **Phase 1 uses only the research period, 2020-01-01 to 2025-09-30** (warm-up from 2019-01-01), which has been
  used for H1, H2, H3, H2b and H4. Every Phase 1 result is labelled **"previously used data: exploratory, not
  evidence"**. Passing proves little; failing ends the candidate.
- **The spent holdout year (2025-10-01 to 2026-10-01) is never read, loaded or run** for H2c, not for design,
  settings, screening or checks. H2c never runs with `bot backtest holdout`. In the forward test, candles from
  that year may serve only as indicator history before H2c's start, by the same rule as H2b's forward report.
- **2026-10-01 to H2c's start** is not used for design or choice either.
- **No forward-test results are examined** before this plan is approved and Phase 1 has finished.

## 5. Phase 1: exploratory analysis on previously used data

Same engine, simulator, fees, slippage (doubled for the checks), risk config and walk-forward windows as H2b's
step B. Every run is as deployed (6% breaker halts).

**Measured for H2b and each candidate, per coin, per correlation group and per regime** (`bot.analysis.regime`:
bull, bear, sideways, and high-volatility; the shared function, never forked):
signal days, entries, completed trades, gross and net P&L, fees paid, slippage paid, expectancy per trade (USDT
and in units of planned risk) at normal and doubled costs, win rate (information only), maximum drawdown,
annualised Sharpe and Sortino of daily equity changes, return / maximum drawdown, exposure (time in market).

**Diagnostics (answer the research question, select nothing):**
- C1: correlation of z14, z28 and z56 on real data; the days where the vote and the score disagree, and how
  trades on those days ended.
- C2: the trades only the 90th-percentile pause allows, separately: their count and expectancy.
- C3: the trades on the new coins, separately; their correlation with the original coins' trades.

**Correlated assets.** Coins move together, so trades are not independent. Trades in the same correlation group
that overlap in time are counted as one *cluster*. The report shows trades and clusters, and confidence
intervals use a block bootstrap by week (10,000 resamples).

**Number of variants tested.** On the research period so far: H1, H2 (on 2 coins, then 4), H3, H2b and H4, so
5 strategies and 6 walk-forward runs, each choosing settings from a grid inside every window. Phase 1 adds
C1, C2 and C3: **8 strategies, 9 runs**. The exact number of setting combinations tried is printed in every
report. Because of this, the random-entry bar is raised from the 95th to the **99th percentile** for H2c.

**Gate: a candidate may go to a forward test only if all of these hold** (fixed now):

1. It passes all 7 checks of the selection rule (`bot.backtest.selection`), unchanged, except the random bar at
   the 99th percentile.
2. It makes at least **25% more** out-of-sample trades than H2b on the same windows.
3. Its expectancy per trade after doubled costs is positive and at least **75% of H2b's**.
4. Its worst window's drawdown, as deployed, is at most H2b's **plus 1 percentage point**, and below 6%.
5. Points 2 and 3 hold in **both halves** of the walk-forward period, and in every regime with at least
   10 trades.
6. For C3, the new coins' own trades have positive expectancy after doubled costs.

If more than one passes, the one with the fewest changes goes forward, in the order C3, C2, C1, never the one
with the best return. If none passes, there is no H2c; the result is recorded in `RESEARCH` and H2b continues.

## 6. Phase 2: prospective comparison on new prices (simulated money)

- **Start:** the day Sultan starts it, after Phase 1. Settings and coins are fixed and fingerprinted at the start,
  as for H2b; any change to H2c's code restarts its test from zero.
- **Account:** its own folder `~/forward-h2c`, 10,000 simulated USDT, the same simulator, fees, slippage, risk
  engine and config. It never places orders and never touches H2b's account.
- **Same data for both:** both runners read the same live Binance prices. The verdict does not rest on the two
  live runs: at the end, H2b and H2c are both replayed on **one set of recorded candles** for the comparison
  window, as H2b's forward report already does for its own checks.
- **Comparison window:** from H2c's start to its end. H2b's earlier days still count for H2b's own test, not here.
- **Length:** at least 12 months and at least 30 closed H2c trades, whichever is later, and at most 24 months.
  Outages as in H2b's plan (more than 10% of days missed moves the end date).
- **Early stop: only for failure.** The 6% drawdown breaker trips, or a risk-rule violation (an entry without
  `evaluate()`, a wrong size): H2c fails. There is no early stop for good results.
- **Memory:** the 512 MB server showed 49% memory and 33% swap in use on 26 Oct. A second forward runner starts
  only if a measured check (like `bot backtest h4-memory`) shows room for it.

**Acceptance (all judged once, at the end; fixed now):**

1. H2b's six forward checks, applied to H2c: positive expectancy after doubled costs; the 6% breaker never trips;
   at least 30 closed trades; positive without the best coin; positive without the best month; beats random
   entries with the same exits on the same prices (here at the **99th** percentile).
2. More signal days and more completed trades than H2b over the same window (at least 25% more trades).
3. Maximum drawdown at most H2b's plus 1 percentage point over the same window.
4. Positive without the best *correlation cluster* (section 5), not only without the best coin.
5. For C3: the new coins' trades have positive expectancy after doubled costs. A 90% block-bootstrap interval is
   reported with it. If its lower end is below zero, the report says "not demonstrated", whatever the point.

**What a pass means:** H2c becomes a candidate, nothing more. It still needs Sultan's decision; any live use
would face the same minimum-order limit (section 8), and no capital or risk change follows from it.

## 7. Sample size: what this test can and cannot show

- 30 trades is a floor for "it works at all", not proof of "better than H2b".
- Illustration (assumed numbers, not results): if one trade's result varies by about 1.5x the planned risk, then
  showing that the *extra* trades have positive expectancy of 0.2x the planned risk (5% one-sided, 80% power)
  takes about **350 trades**; showing that H2c beats H2b by that margin takes about **880 trades per strategy**.
  H2b made about 2 trades a month in walk-forward; C3 might add about 1 more. Neither number is reachable in
  24 months.
- So Phase 2 can show that H2c **is not worse and works on its own**. It **cannot** show that it is better.
  No report may call H2c "better" from these trades, and no claim is made from a few trades.

## 8. What stays exactly as it is

Risk limits (`CEILINGS`, `FLOORS`, `config/default.yaml`), `evaluate()` and `_verify()`, order validation,
protected entries, reconciliation, unknown-order handling, balance checks and accounting, data-freshness checks,
key-permission checks, the live switch, the live capital (100 USDT) and its ceiling, risk per trade (0.25%),
H2b's code (`h2b_blend.py`, `strategies/base.py`) and its forward test. No live trading, no real orders.

At 100 USDT the live account can only take stops up to about 3-4% (`bot entries`, live rules). More signals
from H2c would mostly be refused there for the minimum order. That is a separate decision for Sultan.

## 9. Limitations

- **Reused data:** the research period has served 5 strategies; any pattern in it may be luck. Phase 1 can only
  rule candidates out.
- **Survivorship:** the screen sees only coins listed today; delisted coins are missing, which flatters C3.
- **A post-hoc change:** C3's listing-date change was chosen knowing the first screen found 2 coins.
- **Regimes:** 2020-2025 was mostly rising for crypto. A forward period that falls will show few trades for any
  long-only momentum design, H2b and H2c alike.
- **Correlation:** the coins move together; the effective number of independent trades is lower than the count.
- **Costs are modelled:** the simulator charges the stop-slippage allowance on every market sale (cautious), but
  real fills may differ.
- **Server:** 512 MB limits how many coins and runners fit.

## 10. Work needed after approval (one small module each, with tests)

1. The candidate strategies as new modules (`bot.strategies.h2c_*`); H2b's files untouched (its fingerprint
   tests prove it).
2. The modified screen for C3, run once, its result written once.
3. A Phase 1 report: diagnostics, clusters, block bootstrap, the variant count, the gate.
4. Only if a candidate passes the gate, and only when Sultan starts it: its forward-test mode and end report.

Phase 1 runs on the server; its results come only from those runs.

## 11. Sultan's decisions

- [ ] Approve the research question, the three candidate definitions (section 3) and the data rules (section 4).
- [ ] Approve the Phase 1 gate (section 5), including the 99th-percentile random bar.
- [ ] Approve C3's one screen change (listing date 2021-10-01 instead of 2019-10-01), or drop C3.
- [ ] Approve the Phase 2 design and acceptance rules (section 6), to be used only if a candidate passes the gate.
