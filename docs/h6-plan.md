# H6 plan: weekly rotation into the strongest coins

Status: **DRAFT, written 9 Nov 2026, waiting for Sultan's approval (section 8).** Written after the H5b
coin check (9 Nov 2026), which showed which coins are volatile or thin, but before any backtest of H6.
Once approved, this document is frozen: changing a coin, a setting or a pass rule after that makes it
a new strategy.

## 1. Why H6

Sultan asked on 9 Nov 2026 for a strategy suited to more coins, not H5 or H5b. H5 is built for one coin
at a time (each coin's own breakout). With many coins, a different idea fits better: **each week, hold
the few coins that have been rising the most, and only while they are in an uptrend.** This is
cross-sectional momentum ("relative strength"), one of the most studied effects in crypto returns
(for example, Liu, Tsyvinski and Wu, "Common Risk Factors in Cryptocurrency", Journal of Finance,
2022). It suits volatile coins, because it waits for strength instead of buying dips, and it goes to
cash when nothing is in an uptrend.

Honest link to past results: H2 and H2b are also momentum strategies, but each looked at one coin on its
own. H2 failed the holdout. H6 ranks coins against each other, which is a different rule, but the same
family of idea, so a pass here is weaker evidence than it looks (see section 5).

## 2. The coins

| Coin | In H6? | Why |
| --- | --- | --- |
| BTC, ETH, XRP, BNB | yes | H5's coins |
| SOL, DOGE | yes, **only with Sultan's decision 1** | failed the design package's stability screen (too volatile; SOL also fell 74%) |
| TRX, ZEC | yes, **only with Sultan's decision 1** | failed the liquidity screen; the risk engine still refuses any order on a day below 20M USDT volume (R-11, unchanged) |
| HYPE | **no** | Binance 4h history only from 24 Sep 2026: nothing to test it on |
| XMR | **no** | not traded on Binance Spot |

**Decision 1 is a real exception to the design package:** its stability screen exists to keep the bot
away from coins whose prices can gap far past a stop. H6 does not remove that risk; section 3 only makes
the bot more careful with these coins. Every coin still needs 4h history from 1 Oct 2021 or earlier
(so it covers all test windows), as in H5b.

Correlation groups (R-08) are measured over 2022-10 to 2025-10, as in `bot.backtest.universe`: 0.8 or
more with BTC joins "majors"; otherwise the coin has its own group. Memory: as in H5b, if all eight do
not fit, the new coins with the lowest median volume are left out first, decided once and recorded.

## 3. What H6 is

Data: 4h candles turned into daily candles (finished UTC days only), as H5.

**Once a week,** at the first 4h close after Monday 00:00 UTC:

1. **Uptrend filter:** a coin can be held only if its daily close is above its `trend_days`-day
   average close.
2. **Ranking:** among the coins that pass, rank by return over the last `lookback_days` days.
3. **Hold the top 2.** A coin outside the top 2, or below its trend filter, is sold at market. A new
   top-2 coin that is not held is bought (a limit order at the next open, as every strategy, through
   `evaluate()`). Nothing else changes during the week.
4. **Stop-loss:** `stop_atr` x daily ATR(20) below the entry, placed with the buy; **take-profit**
   3 x the stop distance (as every strategy). After a stop or take-profit, the coin can come back only
   at a later weekly ranking.
5. **Time stop:** 35 days (five weekly rankings).

If no coin passes the uptrend filter, H6 holds nothing (cash).

### Settings

| Setting | Value | Tunable? |
| --- | --- | --- |
| `lookback_days` | 28 | yes |
| `trend_days` | 100 | yes |
| `stop_atr` | 2.5 | yes |
| Coins held | top 2 | fixed |
| Ranking day | Monday 00:00 UTC | fixed |
| ATR period, take-profit, time stop | 20 days, 3R, 35 days | fixed |

Three tunable settings, the project's maximum. The values are common choices for weekly crypto
momentum (4-week return, 100-day trend), chosen before any test, not fitted.

### Extra care for the four coins of decision 1

- **Stop slippage allowance 2.0%** for SOL, DOGE, TRX and ZEC (1.0% today for coins other than BTC):
  the risk engine assumes a worse stop fill, so it buys **less**. This only tightens sizing.
- Every risk limit unchanged: 0.25% risk per trade, at most 3 open positions, at most 2 per
  correlation group, 25% of the account in one coin, the daily and weekly loss limits, the 6%
  drawdown breaker, and R-11's 20M USDT daily volume minimum at order time.

## 4. Costs

The simulator's own costs, unchanged: 0.10% taker fee per fill, 0.01% half-spread, entry slippage
0.05%, stop slippage as above. The pass rules use doubled costs.

## 5. How H6 is tested

The same walk-forward as H5: research period 2020 to Sep 2025, test windows Jan 2022 to Sep 2025
(24-month training, 6-month tests). The holdout year is spent and is never read. All of these must pass:

1. positive expectancy per trade after doubled costs;
2. every test window, as deployed, stays below the 6% drawdown breaker;
3. at least 100 out-of-sample trades;
4. not driven by one coin; 5. not driven by one month;
6. no setting moved by 20% makes the result collapse;
7. **beats random entries with the same exits above their 99th percentile** (not the 95th: H6 is the
   11th strategy backtested on this research period, so the bar is raised for luck);
8. **the four coins of decision 1 earn their place:** out of sample, with doubled costs, their trades
   together make money. Otherwise H6 is re-judged without them only if Sultan approves that as a new
   plan; it is never quietly narrowed.
9. **better than holding the coins:** H6's out-of-sample return beats an equal-weight buy-and-hold of
   the same coins over the same windows with a smaller maximum drawdown.

If any check fails, H6 is dropped, not tuned and re-run. H5 and H2b carry on unchanged.

## 6. After the backtest (only if it passes, and only with Sultan's approval)

1. **Testnet first**, in the same runner as H2b and H5 (one ledger; a coin held by one strategy is never
   bought by another). Note: the Binance testnet may not list every coin; any coin it lacks is tested
   only in a forward test with simulated money (verify on testnet).
2. **Live:** Sultan's switch only, with an H6 capital Sultan sets (like `h5-enable`). Sultan decides how
   H6 shares the 514 USDT with H5 and H2b.
3. A forward test on new prices can run alongside, if Sultan wants one.

## 7. What it can and cannot do (honest expectations)

- About one change a week at most; often nothing (cash) in falling markets like now.
- Momentum crashes: when a market turns sharply, the strongest coins can fall the hardest. The
  uptrend filter and stops limit this but do not prevent it.
- SOL, DOGE, ZEC and TRX can gap past their stops. A gap loses more than 0.25% of the account.
- Nothing here is a result. Whether H6 works after costs is unknown until the backtest runs on the server.

## 8. Decisions for Sultan

1. **Include SOL, DOGE, TRX and ZEC** although they failed the design package's coin screens, with the
   extra care in section 3 (or leave them out: then H6 runs on BTC, ETH, XRP and BNB only).
2. **The rules and settings** in section 3, and the pass rules in section 5 (including the 99th
   percentile, check 8 and check 9).
3. **What a pass leads to** (section 6): testnet first, then live only by Sultan's switch and capital.
