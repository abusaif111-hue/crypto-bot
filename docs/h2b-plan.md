# H2b plan: blended momentum with a volatility pause

Status: **APPROVED by Sultan on 23 Oct 2026 (all four decisions in section 6). Frozen.**
The approving commit is the record of what was decided before any H2b result existed. Changing anything below after that, including
a setting, a coin, a date or a pass rule, makes it a new strategy with a new forward test.

## 1. Where H2 stands

- H2 passed walk-forward on BTC, ETH, XRP and BNB (Jan 2022 to Sep 2025: 121 out-of-sample
  trades, +327.55 USDT, +2.71 USDT per trade, +1.05 with doubled costs).
- H2 **failed** the one-time holdout (Oct 2025 to Oct 2026, run by Sultan on 23 Oct 2026):
  27 trades, −51.65 USDT, −2.16 USDT per trade, −4.05 with doubled costs, 30% win rate.
  Walk-forward and holdout together, XRP (−131.77) and BNB (−44.97) lost money.
- Under the rules approved beforehand, **H2 is rejected** and is not adjusted and re-run.
  **The holdout year is spent**: we have seen it, so it can no longer test anything.

H2b is a **new hypothesis**, not a repair of H2. I proposed it after seeing H2 fail, so
its design is not independent of that result. That is why the backtest below counts only
as a sanity check, and the real test uses prices nobody has seen yet.

## 2. What H2b is

Two changes to H2, both standard ideas from published momentum research, not drawn from
the holdout year:

1. **Several lookbacks instead of one.** H2 used one 4-week return. H2b looks at 2, 4 and
   8 weeks and trades only when most of them agree. This makes it less dependent on one
   setting and less prone to whipsaws.
2. **No new entries during volatility spikes.** Momentum has worked worse after volatility
   shocks (Moskowitz, Ooi and Pedersen, 2012, and later studies). H2b stays out while a
   coin's volatility is unusually high for that coin.

### Exact rules

Data: 4h candles, turned into daily candles (as H2). H2b decides once a day, at the first
4h close after 00:00 UTC, and sees finished candles only.

For each coin, each day:

- `vol` = 30-day volatility of daily closes (as H2).
- For each lookback L of **14, 28 and 56 days**: return `r_L` = today's close ÷ the close
  L days ago − 1. The lookback is **strong** when `r_L` > `threshold` × `vol` × √L.
- **Volatility pause:** today's `vol` must be below the 80th percentile of that coin's own
  30-day volatility over the **365 days before today**. With fewer than 365 earlier values,
  no entry (fail closed).
- **Entry:** at least **2 of the 3** lookbacks strong, and no volatility pause. Limit buy
  0.10% above the last close (as every strategy).
- **Stop-loss:** `atr_mult` × daily ATR(14) below the entry (as H2).
- **Take-profit:** 3 × the stop distance (as every strategy).
- **Time stop:** 30 days (as H2).
- **Strategy exit:** sell at market when at least **2 of the 3** returns `r_L` are below
  zero (checked once a day). The volatility pause only blocks new entries; it never forces
  an exit.

Every entry still goes through `bot.risk.engine.evaluate()`. No risk limit changes.

### Settings

| Setting | Value | Tunable? |
| --- | --- | --- |
| `threshold` | 0.5 (H2's default) | yes |
| `atr_mult` | 3 (H2's default) | yes |
| Lookbacks | 14, 28, 56 days | fixed |
| Agreement | 2 of 3 | fixed |
| Volatility window, pause level, history | 30 days, 80th percentile, 365 days | fixed |
| ATR, time stop, take-profit | 14 days, 30 days, 3R | fixed |

Two tunable settings (the project allows up to 3). Fewer tunable settings leave less room
to fit the past.

### Coins

BTC, ETH, XRP and BNB, with the same correlation groups as H2: BTC and ETH together, XRP
and BNB each alone. These come from the coin screen, whose rules were fixed before any data
was seen. Dropping XRP and BNB because they lost in the holdout would use the holdout
result to pick coins, so **all four stay**.

## 3. How H2b is tested

### Step A: build and check the code

- `bot.strategies.h2b_blend`: pure, finished candles only, the usual tests (no look-ahead
  at several cut points, at most 3 tunable settings, only proposes `TradeIntent`s).
- History: the volatility pause needs about 400 daily candles (about 2,400 4h candles).
  Binance returns at most 1,000 candles per request, so the market data service must page
  through them (built and tested in this step).

### Step B: sanity check on the research period (2020 to Sep 2025)

- The same walk-forward and the same selection rule (all 7 checks) as H1 to H3, unchanged.
- If H2b fails **any** check, it is dropped. It is not tuned and re-run, and no forward
  test starts. (It may fail the 100-trade check, because requiring 2 of 3 lookbacks and
  pausing in volatile periods means fewer trades. If so, that is the result.)
- Passing proves little: this period has been used many times. It is necessary, not enough.
- **The holdout year is not used for H2b at all**: not for design, not for choosing
  settings, not for checks. H2b never runs with `bot backtest holdout`.
- Settings for the forward test: chosen by the walk-forward's own rule on the last 24
  research months (Oct 2023 to Sep 2025), as for H2's holdout. They are a year old by the
  time the forward test starts; that is the price of not touching the spent year.

### Step C: forward test on new prices (the real test)

- H2b runs in the paper simulator on live Binance prices, with simulated money only
  (10,000 USDT), its own account and state folder (`~/forward-h2b`), and the same
  simulator, fees, slippage and risk engine as paper trading.
- **Start:** the day Sultan starts it. Only trades opened after that day count.
- **Length:** at least 12 months, and at least 30 closed trades. It ends at whichever comes
  later, but at **24 months at most**. H2 made 27 trades in 12 months on these four coins,
  and H2b is expected to trade less, so the test will probably take more than 12 months.
- **During the test nothing about H2b changes.** Fixing a bug outside the strategy (the
  runner, the data service) is allowed and recorded in the journal. Any change to H2b
  itself restarts the test from zero.
- **Early stop:** only for failure. If the 6% drawdown breaker trips, H2b fails at once.
  A risk-rule violation (an order that skipped `evaluate()`, a wrong size) stops the test;
  after the fix it restarts from zero. There is no early stop for good results.
- **Outages:** missed days are recorded. If the runner misses more than 10% of the days,
  the end date moves by the missed time.

### Pass rules (all must pass, judged once at the end)

| # | Check |
| --- | --- |
| 1 | Positive expectancy after doubled costs: net P&L per trade, minus one more round of fees and slippage per trade, is above zero |
| 2 | The 6% drawdown breaker never trips |
| 3 | At least 30 closed trades |
| 4 | Not driven by one coin: total profit positive, and still positive without the best coin |
| 5 | Not driven by one month: total profit positive, and still positive without the best month |
| 6 | Beats random entries with the same exits on the same forward prices (above their 95th percentile per trade) |

Check 6 replays the forward period's recorded prices through the backtester once the test
has ended. For information only, the report also shows cash and buy-and-hold BTC over the
same months.

### What the result means

- **Pass:** H2b becomes a candidate. It still needs Sultan's decision, then Stage 5 (testnet,
  5 clean round trips) and Stage 6 (paper-trading review: 8+ weeks, 30+ trades, 5+ profitable,
  zero rule violations). The forward test does not count toward Stage 6.
- **Fail:** H2b is rejected. No H2c is tested on the same forward prices; a new idea needs
  its own forward period.

## 4. What to expect

These are H2's figures, not H2b's; H2b has no results yet.

- H2 out of sample (walk-forward, Jan 2022 to Sep 2025): +327.55 USDT on 10,000, about
  **+3.3% in 3¾ years, under 1% a year**. Over the whole research period with default
  settings (not out of sample) it was +3.2% a year. In my last message I gave "about 2–4% a
  year"; the out-of-sample figure is lower than that.
- The risk rules (0.25% risk per trade, 6% drawdown breaker) keep both losses and gains small.
  Even if H2b works, expect returns of the same order. Buy-and-hold BTC returned far more
  over 2020 to 2025 (+1,480%), with a 77% drawdown that the risk rules exist to avoid.
- This is the fourth strategy idea (H1, H2, H3, H2b). Each new idea raises the chance that
  one passes by luck, which is another reason the forward test is the deciding step.

## 5. Work needed (one small module each, with tests)

1. **H2b strategy** (`bot.strategies.h2b_blend`) and paged candle history; `bot backtest run
   --strategy H2b`.
2. **Holdout lock, tightened:** today the holdout log counts runs per strategy, so a first
   `bot backtest holdout --strategy H2b` would not be marked VOID. Change it so that once any
   strategy has used the holdout, every later run, for any strategy, is VOID. This only makes
   the lock stricter; the pass rules do not change.
3. **Strategy exits in the paper runner:** today the runner only asks strategies for entries
   (`propose`). H2b also needs its daily exit check (`manage`), as in the backtester.
4. **Forward-test mode:** `bot forward run|status|report`, a separate state folder and service,
   labelled "forward test, simulated money" everywhere, and the end-of-test report with the
   6 checks above. It cannot change the paper-trading account and never places real orders.

Steps B and C run on the server; results come only from those runs.

## 6. Sultan's decisions

- [x] Approve H2b as defined in section 2 (rules, settings, coins).
- [x] Approve the test plan and pass rules in section 3.
- [x] Approve a forward-test mode for a strategy that has not passed the full selection rule.
      The design package allows paper trading only for strategies that passed; this is a
      separate, labelled exception with simulated money only.
- [x] Approve tightening the holdout lock (section 5, item 2).
