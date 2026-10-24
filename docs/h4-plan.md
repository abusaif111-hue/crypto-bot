# H4 plan: hourly pullbacks in an uptrend (short-term trading)

Status: **APPROVED by Sultan on 24 Oct 2026 (decisions 1, 2 and 3, and option (a) for decision 4,
in section 7). Frozen.** Written on 24 Oct 2026 before any 1-hour price history was downloaded or
looked at. The approving commit is the record of what was decided before any H4 result existed.
Changing anything below after that (a setting, a coin, a date or a pass rule) makes it a new
strategy with a new forward test.

## 1. Why H4

Sultan asked for a strategy that buys and sells more often than H2b. H2b looks at daily candles
and buys only after weeks of steady rises: about 104 trades on four coins in the walk-forward
years. H4 checks every hour and holds a trade for hours up to 3 days.

H4 is a new idea, not a change to H2b. H2b keeps running unchanged, live and in its forward
test. H4 gets its own tests and its own forward test, and is never mixed into H2b's results.

## 2. Costs come first

Short trades pay the same costs as long ones but have smaller moves to pay them from. These are
the simulator's own costs (`bot.paper.fills`, `bot.backtest.replay`, `config/default.yaml`), the
same ones every backtest and paper trade uses:

| Cost | Buy | Sale at market (strategy exit, time stop, stop-loss) | Take-profit (limit order) |
| --- | --- | --- | --- |
| Taker fee | 0.10% | 0.10% | 0.10% |
| Half-spread | 0.01% | 0.01% | 0.01% |
| Slippage | 0.05% | 0.5% BTC, 1.0% ETH, XRP, BNB | none |

So one round trip costs about **0.8% on BTC and 1.3% on the other coins** when the trade ends with
a market sale, and about 0.3% when it ends at the take-profit. With doubled costs, which the pass
rules use, these double. A trade that aims for a 0.5% move cannot pay this. That is why H4 does
not scalp: it aims for moves of several percent over 1 to 3 days. The costs are not lowered for
H4. The stop-slippage allowance is also what the risk engine sizes on, and lowering it would
weaken a risk setting.

## 3. What H4 is

A standard short-term idea: in a coin whose daily trend is up, buy a sharp hourly drop and hold
it for the rebound. The trend filter is the difference from H3, which bought dips only in flat
(range) markets and did not qualify.

### Exact rules

Data: 1-hour candles, with daily candles built from them (days start at 00:00 UTC). H4 decides
once an hour, after each 1h candle closes, and sees finished candles only.

For each coin:

- **Uptrend (daily, finished days only):** the last daily close is above its 50-day average, and
  that average is higher than it was 10 days earlier.
- **Sharp drop (hourly):** the z-score of the last 1h close against the last 24 hourly closes
  (close minus their average, divided by their standard deviation) is below −`dip`, and the
  hour before was not. Only the first hour of a drop counts, so one drop gives one entry.
- **Entry:** uptrend and sharp drop. Limit buy 0.10% above the last close (as every strategy).
  No new buy while the coin is already held (as every strategy).
- **Stop-loss:** `stop_atr` × daily ATR(14) below the entry.
- **Take-profit:** 3 × the stop distance (as every strategy).
- **Time stop:** 72 hours.
- **Strategy exit:** sell at market when the uptrend ends: checked once a day, at the first 1h
  close after 00:00 UTC, when the last daily close is below its 50-day average.

There is no early "it bounced, sell now" exit, because every market sale costs 0.6% to 1.1%
(section 2). Trades end at the take-profit, the stop, the time stop or the end of the uptrend.

Every entry still goes through `bot.risk.engine.evaluate()`. No risk limit changes: 0.25% risk
per trade, at most 3 open trades, at most 25% of the account in one coin, the 6% drawdown breaker,
and daily and weekly loss limits.

### Settings

| Setting | Value | Tunable? |
| --- | --- | --- |
| `dip` | 2.0 (standard deviations) | yes |
| `stop_atr` | 1.0 (daily ATRs) | yes |
| Trend filter | 50-day average, rising over 10 days | fixed |
| Drop window | 24 hourly closes | fixed |
| ATR, time stop, take-profit | 14 days, 72 hours, 3R | fixed |

Two tunable settings (the project allows up to 3), for the same reason as H2b: fewer settings
leave less room to fit the past.

### Coins

BTC, ETH, XRP and BNB: the coins from the screen whose rules were fixed before any data was seen.
The correlation groups are the same as H2b's: BTC and ETH together, XRP and BNB each alone. No
other coin is added. Picking coins by how H4 does on them would be fitting the past.

## 4. How H4 is tested

### Step A: build and check the code

- `bot.strategies.h4_pullback`: pure, finished candles only, with the usual tests: no look-ahead
  at several cut points, at most 3 tunable settings, only proposes `TradeIntent`s, and its fixed
  values pinned by a test.
- **1-hour history:** `bot backtest fetch --interval 1h` (public data, no API key) from 2019-01-01,
  with the same checks as the 4h data.
- **Memory:** 1h data has 4 times as many candles as 4h. A 4h backtest of four coins needs about
  103 MB; 1h may need about 4 times that. Step A measures it on the server before any run.
  The backtest refuses to start when memory is short (as now). See decision 4 in section 7.

### Step B: walk-forward on the research period (2020 to Sep 2025)

- The same walk-forward and the same selection rule (all 7 checks) as H1 to H3 and H2b,
  unchanged: positive expectancy after doubled costs, every test window below the 6% breaker as
  deployed, at least 100 out-of-sample trades, not driven by one coin or one month, no collapse
  when a setting moves 20%, and better than random entries with the same exits.
- If H4 fails **any** check, it is dropped. It is not tuned and re-run, and no forward test starts.
- Passing proves little, because this period has been used many times. It is necessary, not enough.
- **The holdout year is not used for H4 at all:** not for design, settings or checks. H4 never
  runs with `bot backtest holdout`. The holdout year is spent.
- Settings for the forward test are chosen by the walk-forward's own rule on the last 24 research
  months (Oct 2023 to Sep 2025), as for H2b.

### Step C: forward test on new prices (the real test)

- H4 runs in the paper simulator on live Binance prices, with simulated money only (10,000 USDT).
  It has its own account, state folder (`~/forward-h4`) and service, separate from H2b's forward
  test, and the same simulator, fees, slippage and risk engine.
- **Start:** the day Sultan starts it. Only trades opened after that day count.
- **Length:** at least 12 weeks and at least 60 closed trades, ending at whichever comes later,
  and at most 12 months. Fewer than 60 closed trades at 12 months is a fail. H4 should trade more
  often than H2b, so it can be judged sooner. How often it really trades is unknown until step B.
- **During the test nothing about H4 changes.** Fixing a bug outside the strategy (the runner,
  the data service) is allowed and recorded in the journal. Any change to H4 itself restarts the
  test from zero (the runner checks H4's code fingerprint, as for H2b).
- **Early stop:** only for failure. If the 6% drawdown breaker trips, H4 fails at once. A risk-rule
  violation stops the test, and after the fix it restarts from zero. There is no early stop for
  good results.
- **Outages:** missed hours are recorded. If the runner misses more than 10% of the time, the end
  date moves by the missed time.

### Pass rules (all must pass, judged once at the end)

The same six checks as H2b's forward test, with the trade count raised to 60:

| # | Check |
| --- | --- |
| 1 | Positive expectancy after doubled costs: net P&L per trade, minus one more round of fees and slippage per trade, is above zero |
| 2 | The 6% drawdown breaker never trips |
| 3 | At least 60 closed trades |
| 4 | Not driven by one coin: total profit positive, and still positive without the best coin |
| 5 | Not driven by one month: total profit positive, and still positive without the best month |
| 6 | Beats random entries with the same exits on the same forward prices (above their 95th percentile per trade) |

### What the result means

- **Pass:** H4 becomes a candidate. Going live with it is Sultan's decision alone, with the same
  live switch, a capped amount and the same risk engine. H2b's live trading is not affected either way.
- **Fail:** H4 is rejected. No H4b is tested on the same forward prices; a new idea needs its own
  forward period.

## 5. What to expect

H4 has no results yet. These facts bound what is realistic:

- **H3, the closest earlier idea, did not qualify.** It bought oversold 4h dips in flat markets
  with a 24-hour time stop. H1 (4h breakouts) did not qualify either. Their reports are on the
  server (`~/backtests`). H4 differs in the trend filter, the hourly trigger and the longer hold;
  whether that is enough is what step B tests.
- **Costs (section 2):** each trade that ends with a market sale must gain about 0.8% (BTC) to
  1.3% (other coins) just to break even, and twice that under the doubled-cost check.
- **Small amounts:** the risk rules risk 0.25% of the account per trade. On the 300 USDT live
  account that is about 0.75 USDT per trade, so even a good short-term strategy makes small
  amounts. More trades do not mean more profit; they mean more fees.
- **Multiple tries:** this is the fifth idea (H1, H2, H3, H2b, H4). Each new idea raises the chance
  that one passes by luck. That is why the forward test on unseen prices decides, not the backtest.

## 6. Work needed (one small module each, with tests)

1. `bot.strategies.h4_pullback` and 1h history (`bot backtest fetch --interval 1h`);
   `bot backtest run --strategy H4`.
2. The backtester and paper runner on 1h candles: the runner already asks a strategy once per
   finished candle of its interval. Step A checks this end to end for 1h and measures memory.
3. Forward test for H4: `bot forward start --strategy H4 --state-dir ~/forward-h4`, its own service
   (`bot-forward-h4`) and report, reusing `bot.forward` and `bot.forward_report` without changing
   their checks for H2b (H2b's forward test is already running and must not be disturbed).

Steps B and C run on the server, and results come only from those runs.

## 7. Sultan's decisions

- [x] 1. Approve H4 as defined in section 3 (rules, settings, coins).
- [x] 2. Approve the test plan and pass rules in section 4, including the forward test's length
      (at least 12 weeks and 60 closed trades, at most 12 months).
- [x] 3. Approve a second forward test running next to H2b's, with simulated money only.
- [x] 4. If step A shows four coins on 1h candles do not fit the server's memory: (a) test H4 on
      BTC and ETH only, or (b) resize the server for the backtest. Pick one now, before any
      H4 data is seen.
      **Sultan chose (a).** If step A's measurement shows four coins on 1h candles do not fit,
      H4 is tested and forward-tested on BTC and ETH only, decided by the measurement alone and
      before any H4 result. With two coins (one correlation group) H4 makes fewer trades, so the
      100-trade check is harder to pass (H2 failed it on these two coins); if it fails, that is the
      result, and no coin is added afterwards.
