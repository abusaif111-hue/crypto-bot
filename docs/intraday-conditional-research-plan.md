# Intraday conditional-order research protocol (IC): Binance Spot, BTC, ETH, XRP and BNB

Status: **DRAFT revision 1 (27 Oct 2026), for Sultan's review. Not approved.** Gate A (section 14) has not
been passed. No code was written and no data was downloaded for this document. No backtest or simulation was
run. Nothing in the bot, its settings, its services or its accounts was changed.

This is a research protocol only. It decides *how* a future experiment would be run and judged, before any
data for it is seen. It does not claim that an intraday conditional-order strategy works, nor that it would
do better than H2b.

Labels used throughout:

| Label | Meaning |
| --- | --- |
| **[FACT]** | Established: read from this repository, its recorded results or Binance's documentation. |
| **[ASSUMPTION]** | A modelling choice or estimate. It is not measured, and it is stated so it can be challenged. |
| **[PROPOSED]** | A threshold, setting or rule suggested here. It binds only once Sultan approves it at Gate A. |
| **[DECISION]** | A choice only Sultan can make (listed in section 15.2). |
| **[VERIFY]** | A Binance feature or data property that must be checked in the docs or data before it is relied on. |

---

## 1. Executive summary

**Question.** Can a fixed, pre-registered intraday plan generate trades on BTC, ETH, XRP and BNB? Each plan
has a conditional entry trigger, an entry expiry, a stop-loss, a take-profit, a maximum holding time and a
mandatory session-end exit. The protocol asks whether such plans show a positive edge after realistic costs
and missed fills, and whether that edge is large enough to justify further research. "Further research"
means more simulation, never trading.

**Design in one paragraph.**
- Three entry families are compared (breakout, pullback, momentum confirmation), each with two exit
  methods. That makes six pre-registered candidates [PROPOSED].
- Decisions are made on finished 15-minute candles, with 1-hour and daily context built from them [PROPOSED].
- The simulator is pessimistic and candle-based. It never fills where the data cannot prove a trade was
  possible, and when the order of prices inside a candle is unknown, it takes the worse outcome.
- Every simulated entry goes through the existing risk engine (`evaluate()`) with today's settings,
  unchanged.
- The data is split three ways: development, validation, and a test period nobody has seen. Two
  windows are excluded entirely: the spent holdout year and the intraday plan's locked window. So the
  test period proposed here is prospective: new 15-minute prices recorded after the protocol is frozen,
  replayed offline once enough have accumulated [DECISION].
- Execution reliability is a separate experiment (C), using fault injection. Its pass bar is correctness,
  not profit.

**Two facts set the bar before any data is seen.**
1. **Costs and the risk engine [FACT].** The engine requires a reward-to-risk of at least 1.5 *after*
   entry slippage, stop slippage (0.5% BTC, 1.0% ETH, XRP and BNB) and 0.1% fees each side
   (`src/bot/risk/engine.py`, `config/default.yaml`). By that formula, a plan with a stop 1% below entry
   needs a take-profit about **2.9% above entry on BTC** and about **3.6% on the other three coins**
   (worked out in section 8.4). Intraday moves of that size are uncommon, so many plans will be rejected
   or will rarely reach their target. This is a property of the existing, unchanged risk settings. The
   protocol reports it; it does not lower the settings.
2. **Prior results [FACT].**
   - H1 and H3 did not qualify.
   - H4 (hourly pullbacks in a daily uptrend) did not qualify: 331 out-of-sample trades, −1.51 USDT per
     trade, no better than random entries.
   - H2 failed the holdout.
   - The paused intraday plan (I1 to I3) was never run.

   Every new idea tested on the same history raises the chance that one passes by luck. Section 10.4
   therefore uses a raised bar and a formal multiple-comparison control.

**Outcome classes.** The experiment ends as one of four classes:
- **QUALIFIED FOR FURTHER RESEARCH** (never "approved for trading");
- **REJECTED**;
- **INCONCLUSIVE**: too little evidence. The bar is never lowered afterwards;
- **VOID**: a rule was broken, for example a look-ahead was found or the test period was opened early.

**What Sultan decides first.** Section 15.2 lists the open decisions. The most important:
- the test period;
- the coins and server memory: four coins on 15-minute data do not fit the 512 MB server in today's format
  (section 5.6);
- how this protocol relates to the paused intraday plan and to the rejected H4.

---

## 2. Scope and exclusions

**In scope (each part only after its own gate):**
- a written protocol (this document; Gate A);
- read-only data-quality tooling (Gate B);
- downloading historical candles (Gate C);
- an isolated simulator (Gate D);
- offline backtests on the development and validation periods (Gate E), and later the test period;
- a results review (Gate F).

**Out of scope, always:**
- live or testnet trading of any IC candidate; any order sent to Binance, including test orders;
- any change to H2b, `h2b_blend.py`, `strategies/base.py`, H2b's forward test, `forward.json` or its
  report checks;
- any change to risk limits (`CEILINGS`, `FLOORS`, `config/default.yaml`), account settings, credentials,
  services, the live switch or the live cap;
- any change to the H2c protocol (`docs/h2c-plan.md`, revision 7) or its approval status;
- any change to the intraday plan (`docs/intraday-plan.md`, frozen and paused), its locked final window,
  or the spent holdout year;
- PR-2, PR-3 and other remediation work;
- margin, futures, leverage, borrowing or shorting. The protocol is long-only Spot, USDT pairs.

**Not delivered by this document:** code, data, results, or a recommendation to trade.

---

## 3. Existing-system constraints

These are **[FACT]**s from the repository (CLAUDE.md, `config/default.yaml`, `bot.backtest.periods`,
`docs/*`). The protocol must respect all of them.

| Area | Constraint | Consequence for IC |
| --- | --- | --- |
| H2b | Frozen; forward test running (12 to 24 months, at least 30 trades); trades live with a capped 100 USDT | IC never reads, writes or replays H2b's files or accounts; its results are never mixed with H2b's |
| Spent holdout | 2025-10-01 to 2026-10-01; no strategy may be judged on it again | IC never loads it (the existing lock in `bot.backtest.periods` stays) |
| Intraday plan's locked window | 2024-10-01 to 2025-10-01, reserved for I1 to I3, opened once by Sultan | IC never loads it while that plan exists. Using it would void the paused plan's final test [DECISION 3] |
| Intraday research | Paused 25 Oct 2026: 15m data for BTC since 2019 needs about 260 MB, above the server's free memory | IC faces the same limit, four times over, unless the format, the machine or the scope changes [DECISION 2] |
| H4 | Rejected; never re-tested with changed settings, coins or a regime filter | IC's pullback family must differ in substance from H4, or be dropped [DECISION 4] |
| Order path | Every entry through `evaluate()`; simulator entries only via `SimExchange.submit()`; research code must not import `bot.execution` or `bot.exchange.trading` | The IC simulator is offline and checked by an architecture test (section 9.5) |
| Risk settings | risk per trade 0.25%; daily loss 1.5%; weekly loss 3%; at most 3 open positions; open risk ≤ 1.5%; invested ≤ 50%; one coin ≤ 25%; at most 2 per correlation group (correlation above 0.8); drawdown breaker 6%; reward-to-risk ≥ 1.5 after costs; spread ≤ 0.10%; entry slippage 0.05%; stop slippage 0.5% BTC, 1.0% default | Used unchanged in the simulation. IC may add stricter research-only rules (section 9), never looser ones |
| Strategy conventions | Finished candles only; at most 3 tunable settings; strategies only propose `TradeIntent`s | Each IC candidate has at most 3 tunable settings (section 6) |
| Day boundary | 00:00 UTC (04:00 Muscat); daily loss limits reset there | Proposed session end is the same boundary (section 6.3) |
| Results | Only from real runs on the server; never estimates | This document contains no results |

---

## 4. Research questions and hypotheses

### 4.1 Questions

- **Q1.** After pessimistic execution and the base cost scenario, does any of the six candidates have
  positive expectancy per trade on validation? Does that still hold under the adverse scenario?
- **Q2.** Does the conditional trigger add anything beyond its context? Each candidate is compared with
  a *context-only baseline*: the same context and exits, but an entry at the next open whenever the context
  first holds in a session. It is also compared with random entries using the same exits.
- **Q3.** How much of the gross result is lost to fees, spread, slippage, gaps and missed fills? How
  sensitive is the net result to each?
- **Q4.** Do results depend on one coin, one month or one market regime?
- **Q5.** How often do plans fail to execute (expired, missed, rejected by the risk engine, especially
  for reward-to-risk), and does that change the conclusion?
- **Q6 (Experiment C).** Does the simulator's order-state handling stay correct under every injected fault?

### 4.2 Hypotheses (pre-registered; one per candidate)

For each candidate *k* (section 6.5):

- **H0k (null):** expected net result per trade, after adverse costs, is ≤ 0, *or* it is no better than
  the 99th percentile of random entries with the same exits.
- **H1k (alternative):** expected net result per trade after adverse costs is > 0, *and* it beats the
  99th percentile of random entries *and* the candidate's context-only baseline.

No hypothesis is added, removed or redefined after Gate C data is downloaded. A seventh idea is a new
experiment with its own document.

---

## 5. Required data and quality checks

### 5.1 What is needed

| Data | Coins | Period | Source | Status |
| --- | --- | --- | --- | --- |
| 15m candles (decisions) | BTCUSDT, ETHUSDT, XRPUSDT, BNBUSDT | 2019-01-01 to 2024-10-01, then the prospective test period | Binance klines (REST through the read-only adapter, or monthly files on data.binance.vision) | [FACT] available, as already used for daily and 1h data; 15m for BTC was fetched in intraday step 1 |
| 1h and 1d context | same | same | **Built from the 15m candles** (`bot.analysis.resample`; a finished period only) | [FACT] the function exists and is tested (`resample(cs, DAY_MS) == daily(cs)`) |
| 1m candles (resolving ambiguous candles only) | same | only the 15m candles where the order of prices matters (section 7.3) | data.binance.vision monthly 1m files | [VERIFY] size and availability per coin; [DECISION 6] whether to use them at all |
| Exchange rules (tick, step, minimum order) | same | as today | `exchangeInfo` (read-only) | [FACT] current rules only; [ASSUMPTION] past rules are taken to equal today's (section 5.4) |
| Bid-ask spread | same | — | not in candle data | see 5.3 |
| Fees | — | — | Binance fee schedule; the account's own commission (`GET /api/v3/account/commission` via the read-only adapter) | [FACT] 0.1% taker on the account today, BNB discount off (intraday plan section 6); [ASSUMPTION] the same for the whole history |

Each kline carries open time, open, high, low, close, volume, close time, quote volume, number of trades,
taker-buy base volume and taker-buy quote volume [FACT, Binance kline format]. **No bid, ask or order-book
depth is in candle data.**

### 5.2 Time and timestamps

- All times are UTC, in integer milliseconds. An interval is identified by its open time.
- A 15m candle exists for a decision at time *t* only if its **close time is < t**. It is the same
  "finished candle" rule the bot already uses, tested at several cut points.
- **[VERIFY]** Binance has published spot files on data.binance.vision with timestamps in microseconds
  from 2025-01-01 onward. The loader must detect the unit per file, and the quality report must state it.
  This matters only for the prospective test data, since the development and validation data is all
  before 2025.
- A 1h or 1d context value is usable only after the last 15m candle of that hour or day has closed.

### 5.3 Spread, liquidity and volume

- **Spread history is not available from candles [FACT].** To my knowledge, the spot historical files
  on data.binance.vision hold klines, trades and aggregated trades, not best bid and ask **[VERIFY]**.
- Three options, in increasing cost [DECISION 7]:
  - **(a)** a constant half-spread (today's simulator uses 0.01%), with stress scenarios (section 8);
  - **(b)** an estimate from candles (for example the Corwin-Schultz or Abdi-Ranaldo high-low estimators).
    These are noisy estimates, labelled as such, and never shown as measured spreads;
  - **(c)** a read-only recorder of the public best bid and ask during the prospective test period. The
    spread for the test period is then measured, not assumed. It needs Gate B approval and
    touches no account.
- Liquidity filter: the existing R-11 (spread ≤ 0.10%) and the 24-hour quote volume minimum
  (20,000,000 USDT) are applied point-in-time from candles: quote volume over the previous 96 finished
  15m candles. A candle with zero volume or zero trades never produces a fill.
- **Order size versus volume [PROPOSED]:** a simulated order may fill at most **1%** of the trigger
  candle's base volume. Any excess is unfilled (section 7.5). At the 100 USDT budget this almost never
  binds. At 10,000 USDT it occasionally may on XRP or BNB.

### 5.4 Symbol and market availability

- **[FACT]** All four pairs have daily data in the repository's research history (H2 used them from
  2020).
- **[VERIFY]** The first 15m candle of each pair is read from the data. A coin is used only from its
  first full month.
- **[ASSUMPTION]** Tick and step sizes and the minimum order have changed over the years, and past
  values are not published in `exchangeInfo`. The simulator uses today's rules throughout. Rounding is
  always down (quantities) and toward the worse side (prices). The report says so.
- **Survivorship [FACT]:** the four coins were chosen knowing they still trade today. Results are
  conditional on that.

### 5.5 Quality checks (Gate B tooling; read-only; each failure is reported, never silently fixed)

| # | Check | Action on failure |
| --- | --- | --- |
| Q-1 | Every open time is a multiple of 15 minutes (UTC ms) | Reject the file |
| Q-2 | No duplicate open times; if duplicates are identical, keep one and count them; if they differ, stop | Stop on any conflict |
| Q-3 | Gaps: list every missing 15m candle and each run of missing candles, with start, end and length | Report. The simulator treats a gap as an outage (section 7.8). Coverage below the threshold in section 12 fails |
| Q-4 | OHLC consistency: low ≤ min(open, close); max(open, close) ≤ high; all > 0; volume ≥ 0 | Reject the candle (an outage); count |
| Q-5 | Cross-check: four 15m candles rebuilt into an hour equal Binance's own 1h candle (open, high, low, close exactly; volume within rounding); the same for days against 1d | Stop if more than the threshold in section 12 differ |
| Q-6 | Outliers: a candle whose high-low range is more than 10x the median range of the previous 96 candles, or a close more than 20% from the previous close [PROPOSED] | Flagged and listed, **kept** (real crashes happen), and shown in the report |
| Q-7 | File integrity: the checksum Binance publishes beside each monthly file matches [VERIFY] | Reject the file |
| Q-8 | Stale data: identical OHLC with zero volume for longer than 1 hour | Treated as an outage |
| Q-9 | Locked windows: no candle with open time in 2024-10-01 to 2026-10-01 is present in any development or validation file | Stop: the data is void until removed and the loader fixed |

### 5.6 Memory and server [FACT and estimate]

- **[FACT]** A candle takes about 940 bytes in the current in-memory format. That was measured in
  intraday step 1.
- **[ASSUMPTION, arithmetic]** 15m candles from 2019-01-01 to 2024-10-01 are about 201,000 per coin:
  about 190 MB per coin and **about 760 MB for four coins**. That is above the 512 MB server's free
  memory (about 260 MB).
- A joint replay is needed, because the position and correlation limits act across coins. So
  "one coin at a time" changes the experiment.
- Options [DECISION 2]:
  - **(a)** a compact numeric format, about 50 bytes per candle [ASSUMPTION]: a Gate D code change,
    measured before any run;
  - **(b)** a larger machine for research only;
  - **(c)** fewer coins, decided before data is seen;
  - **(d)** a longer decision interval (1h). That is closer to H4's territory (decision 4).
- The protocol never shortens the period or drops a coin after seeing results.

---

## 6. Proposed conditional-order models

### 6.1 Shared rules (all candidates)

- **Decision clock:** after each finished 15m candle, from finished candles only. A plan made at the
  close of candle *t* becomes **active at the open of candle t+1**. Nothing that happened inside
  candle *t* or earlier can trigger it.
- **No trade is planned when any of these holds:**
  - the coin is already held or has a live plan;
  - the risk engine would reject the plan (checked with the plan's own prices);
  - the 1h context does not hold;
  - liquidity filters fail (section 5.3);
  - any data in the lookback is missing;
  - the time is after the entry cutoff (6.3);
  - the coin is in a correlation group already at its limit;
  - an order state is unresolved (section 9.3).
- **Entry expiry [PROPOSED]:** 4 candles (1 hour) after activation, or the entry cutoff, whichever is
  earlier. An expired plan never fills later.
- **One plan per coin at a time [PROPOSED]:** at most 2 filled entries per coin per session.

### 6.2 The three entry families (compared, not ranked in advance)

| Family | Idea | Trigger (conditional order) | Context (1h, finished) | Main risk of illusion |
| --- | --- | --- | --- | --- |
| **B: breakout** | Price leaving a recent range keeps going | **Buy-stop-limit**: trigger *T* = highest high of the last `lookback` 15m candles + 1 tick; limit *L* = *T* × (1 + 0.10%) | 1h close above its 50-hour average | Fills at the top of a spike (gaps through *L* are missed, section 7.4) |
| **P: pullback** | In an established up-move, a dip is bought | **Buy-limit** *P* = last close − `depth` × 15m ATR(16), resting | 1h uptrend: close above its 50-hour average, average higher than 6 hours before | Limit fills on touches the queue would not have reached (section 7.4); overlap with H4 (decision 4) |
| **M: momentum confirmation** | Wait for strength to be proven, then join | **Buy-stop-limit** at *T* = the high of the confirming candle + 1 tick, placed only after `n` consecutive 15m closes above the 20-candle average with rising volume; limit *T* × (1 + 0.10%) | as B | Late entries; costs a larger share of the move |

Notes:
- The fixed values (50-hour average, 6-hour slope, ATR(16), 20-candle average, the 0.10% limit cap) are
  pinned by tests at Gate D, as for H2b and H4. Only the named settings are tunable.
- **Family P versus H4 [DECISION 4].** H4 bought a 1h z-score dip in a *daily* uptrend and held up to
  72 hours. P uses a 15m resting limit in a 1h uptrend, with a session-end exit, so it is a different
  rule set. But the economic idea (buy dips in an uptrend) is the same one H4 failed with.

  Options:
  - **(a)** keep P and report H4's rejection beside it as prior evidence against it;
  - **(b)** drop P, leaving four candidates.

  I recommend (a) only if Sultan accepts that P is a new strategy, not an H4 re-test. Otherwise (b).
- **Family B versus I2 [DECISION 3].** The paused plan's I2 also trades breakouts, on BTC 15m. B differs
  (a conditional buy-stop instead of a confirmed close, four coins, session exits). Running both on the
  same history counts as two tests of the same idea (section 10.4).

### 6.3 Trade plan: exits and timing

| Element | Rule [PROPOSED] |
| --- | --- |
| Session | The UTC day (00:00 to 00:00), the bot's existing day boundary [DECISION 5] |
| Entry cutoff | No plan activates after 20:00 UTC |
| Maximum holding time | 8 hours from the fill |
| Mandatory session-end exit | Any open position is sold at market at the open of the 23:45 UTC candle, with the market-exit costs in section 8, whatever its profit or loss |
| Initial stop | Exit method X1 or X2 (6.4); a sell stop, assumed to execute as a market sale when triggered |
| Take-profit | A resting sell limit at the target (6.4) |
| Same-candle conflicts | If one candle touches both stop and target, the **stop** is taken (as today's simulator does) |

A stop trigger is **not** an execution guarantee. A triggered stop fills at the worse of the stop price
and the next available price (a gap fills at the open), less the stop-slippage allowance (section 7.6).

### 6.4 Two exit methods (fixed in advance, never fitted on the test period)

| Method | Stop | Take-profit | Tunable |
| --- | --- | --- | --- |
| **X1: volatility stop** | `stop_atr` × 1h ATR(24) below the fill | At least the risk engine's minimum: target = the price that gives reward-to-risk exactly `rr_target` after costs (default 2.0) | `stop_atr`, `rr_target` |
| **X2: structure stop** | 1 tick below the lowest low of the last 8 finished 15m candles, capped at 3 × 1h ATR(24) | Same rule as X1 | `rr_target` |

- `rr_target` may never be below the engine's 1.5. A lower value is rejected by `evaluate()` anyway.
- Considered but not proposed: a time-only exit with no take-profit. The risk engine needs a
  take-profit to compute reward-to-risk, so a plan without one cannot be sized.
- Considered but not proposed: trailing stops. Moving the protection is an extra order-state risk,
  and H2b's runner does not support raising a stop.

### 6.5 The six candidates and their settings (at most 3 tunable each)

| Candidate | Entry | Exit | Tunable settings and the values tried on development only [PROPOSED] |
| --- | --- | --- | --- |
| IC-B1 | B | X1 | `lookback` {16, 32, 48}; `stop_atr` {1.0, 1.5, 2.0}; `rr_target` {1.5, 2.0, 3.0} |
| IC-B2 | B | X2 | `lookback` {16, 32, 48}; `rr_target` {1.5, 2.0, 3.0} |
| IC-P1 | P | X1 | `depth` {0.5, 1.0, 1.5}; `stop_atr` {1.0, 1.5, 2.0}; `rr_target` {1.5, 2.0, 3.0} |
| IC-P2 | P | X2 | `depth` {0.5, 1.0, 1.5}; `rr_target` {1.5, 2.0, 3.0} |
| IC-M1 | M | X1 | `n` {2, 3, 4}; `stop_atr` {1.0, 1.5, 2.0}; `rr_target` {1.5, 2.0, 3.0} |
| IC-M2 | M | X2 | `n` {2, 3, 4}; `rr_target` {1.5, 2.0, 3.0} |

That is 27 + 9 + 27 + 9 + 27 + 9 = **108 configurations** in total. Each one is recorded in the trial log
(section 10.3), and the number is part of the multiple-comparison control.

---

## 7. Simulation methodology

The IC simulator is a new, isolated, offline module (Gate D). It reuses the existing conventions of
`bot.paper.fills` and `bot.backtest.replay` (pessimistic fills, the stop taken first). It sends every
entry through `SimExchange.submit()`, so through `evaluate()`. It **cannot reach any Binance endpoint
that places, cancels or tests an order**: it imports neither `bot.execution` nor `bot.exchange.trading`,
and it opens no network connection at all (section 9.5).

### 7.1 Simulated order states

`PLANNED → ACTIVE → (TRIGGERED) → PARTIAL → FILLED`, or `→ EXPIRED`, `REJECTED`, `CANCELED`, or `UNKNOWN`.

These mirror the bot's ledger states (PENDING, OPEN, CLOSED, NOT_OPENED) *by specification*. They are not
imported from it.

`UNKNOWN` exists only in Experiment C (injected faults). In Experiment B, every simulated order has a
definite outcome.

### 7.2 Activation and look-ahead

- A plan made at the close of candle *t* is active from the open of *t+1*.
- [PROPOSED] An optional acknowledgement delay of *d* candles (base *d* = 0; stress *d* = 1) moves
  activation to *t+1+d*.
- Any price inside or before the plan's own decision candle can never trigger it. This is tested with
  data where *t* contains the trigger price (test IC-T1, Gate D).
- Context values come only from finished hours and days (5.2).

### 7.3 Prices inside a candle: what can and cannot be known

- A 15m candle gives four prices and no order. **When the path matters, the worse path is assumed:**
  - entry and stop in the same candle: entered, then stopped;
  - entry and target in the same candle: entered, target **not** reached in that candle;
  - stop and target in the same candle: the stop.
- [DECISION 6] Optionally, an ambiguous candle is resolved with its fifteen 1m candles, applying the same
  worst-path rule inside each minute. This narrows the ambiguity but cannot remove it. The report gives
  results with and without 1m resolution and counts the ambiguous candles.

### 7.4 Entry fills (no impossible fills)

**Buy-stop-limit (B, M)**, trigger *T*, limit *L*:

| Candle while active | Simulated result |
| --- | --- |
| high < *T* | Not triggered |
| open ≥ *T* and open ≤ *L* (gapped above the trigger, within the limit) | Fills at the open + entry slippage, capped at *L* |
| open > *L* (gapped through the limit) | Triggered but **not filled** (missed); counted as "gapped through" |
| open < *T* ≤ high | Fills at *T* + entry slippage, capped at *L*; if *T* + slippage > *L*, not filled |
| expiry reached untriggered | EXPIRED ("never reached") |

**Buy-limit (P)**, price *P*:

| Candle while active | Simulated result |
| --- | --- |
| low > *P* | Not filled |
| low = *P* exactly (touch only) | **Not filled** in the base case: the queue position is unknown. A sensitivity test fills on a touch |
| low ≤ *P* − 1 tick (traded through) | Fills at *P*. No price improvement, even if the candle opened below *P* |
| expiry reached | EXPIRED |

### 7.5 Partial fills

- The quantity filled is the smaller of the order and 1% of the candle's base volume [PROPOSED].
- An unfilled remainder of an entry is cancelled (no chasing). The partial position gets its own stop and
  target sized on the filled quantity.
- If the filled part is below Binance's minimum order or minimum notional, the order is recorded as
  "partial, below minimum" and sold at the next open with market-exit costs. That matches what the bot's
  real entry path would have to do.
- The bot's live entries are fill-or-kill. A **fill-or-kill variant** (partial counts as no fill) is
  reported as a sensitivity.

### 7.6 Exits

- **Stop:**
  - triggered when low ≤ stop;
  - fills at min(stop, open) × (1 − stop slippage) (the existing `stop_fill_price` rule);
  - a gap through the stop fills at the open, less slippage.
- **Take-profit:**
  - fills at the target only if high ≥ target + 1 tick (traded through) [PROPOSED];
  - a touch alone does not fill;
  - no price improvement.
- **Time exit and session-end exit:** a market sale at the open of the next candle, less the market-exit
  slippage (section 8).
- **Rejected orders:** a plan whose quantity after rounding down is below the minimum order or minimum
  notional is REJECTED and counted. So is one whose prices break `PERCENT_PRICE_BY_SIDE` or the tick
  rules (using today's rules, 5.4). It is never "fixed" by rounding up.

### 7.7 Uncertain outcomes, reconciliation and restart (simulated)

These apply to Experiment C. Experiment B runs with every outcome definite.

- A simulated order whose acknowledgement is lost is `UNKNOWN`. The simulator **never re-sends it**.
  It blocks every new entry, on every coin, until a simulated reconciliation settles it. This is the
  same principle as the bot's PR-1 gate.
- Simulated reconciliation decides "never executed" only when every simulated record agrees: the order
  itself, its protective legs, the fills and the balance. Otherwise the order stays `UNKNOWN` and entries
  stay blocked.
- A restart reloads the simulator's state file, keeps all breakers, and blocks entries until the state
  reconciles. An unreadable state file stops the run; it is never reset.

### 7.8 Outages and gaps

- During a gap in the data (Q-3, Q-4, Q-8) nothing triggers, nothing fills and no plan is made.
- Plans expire by the clock.
- Open positions are valued at the first candle after the gap. A stop that the gap jumped over fills at
  the first open after it, less slippage.
- Gaps are listed in the report with the positions they affected.

---

## 8. Transaction-cost assumptions

### 8.1 Measured versus estimated

| Item | Status |
| --- | --- |
| Fee rate | **Measured** for today's account (0.1% taker; BNB discount off). [ASSUMPTION] It applies to the whole history; past fee promotions are ignored, which errs against the strategy |
| Spread | **Estimated** for history (no bid/ask data); **measurable** in the test period only with option 5.3(c) |
| Entry slippage | **Estimated** (0.05%, the risk engine's setting) |
| Stop slippage | **Estimated** allowance (0.5% BTC, 1.0% ETH, XRP, BNB): a deliberately pessimistic risk setting, not a measurement |
| Market exits other than stops | **Estimated**; decision 8 |
| Fills of the bot's own orders | **Measured but tiny**: 5 testnet round trips (testnet liquidity is not representative) and a few capped live trades. Not usable for calibration beyond a sanity check |
| Partial and missed fills | **Modelled** from candle volume and price path (7.4, 7.5); not measured |
| Gaps | **Measured** from the candles themselves |

### 8.2 Three cost scenarios (every result is shown under all three)

| Item [PROPOSED] | Base | Adverse | Stress |
| --- | --- | --- | --- |
| Fee per side | 0.10% | 0.10% | 0.15% |
| Half-spread | 0.01% BTC/ETH, 0.02% XRP/BNB | 0.03% / 0.05% | 0.05% / 0.10% |
| Entry slippage | 0.05% | 0.10% | 0.20% |
| Stop slippage | 0.5% BTC, 1.0% others (today's) | doubled | tripled |
| Other market exits (time, session end) | decision 8 (0.05% if as the intraday plan) | doubled | the full stop allowance |
| Acknowledgement delay | 0 candles | 0 | 1 candle |
| Limit fill on a touch | no | no | no, and also no fill on a 1-tick trade-through (2 ticks needed) |
| Volume cap | 1% | 0.5% | 0.25% |

- The pass rules (section 12.5) use the **adverse** scenario.
- Base is shown for comparison only.
- Stress is shown to see where the result breaks. A candidate need not pass stress, but the report says
  at what cost level its expectancy reaches zero.
- The adverse scenario's doubled costs match the "doubled costs" check used for H1 to H4 and H2b.

### 8.3 What every comparison must show

For each candidate and baseline:
- gross result;
- the cost of fees, spread, slippage and gaps, each separately;
- net result;
- the expected value lost to unfilled conditional orders, as the counterfactual result of missed
  entries. That figure is reported but never added to the net result.

### 8.4 Worked example: what the risk engine already demands [FACT: the formula; arithmetic shown]

The engine's formula (`src/bot/risk/engine.py`, step 9), for entry 100, stop 99 (1% below), 0.05% entry
slippage and a 0.1% fee:
- **BTC** (0.5% stop slippage): loss per unit ≈ 1.744. A reward-to-risk of 1.5 needs a target
  ≈ **102.87 (+2.9%)**.
- **ETH, XRP and BNB** (1.0%): loss per unit ≈ 2.238, needing ≈ **103.61 (+3.6%)**.

Wider stops need proportionally larger targets. This is why `rr_target` starts at 1.5 and why
rejections for reward-to-risk are reported (Q5). The settings are not changed.

---

## 9. Risk and operational safety requirements (research only)

### 9.1 Limits used in the simulation

- **All existing risk settings apply unchanged through `evaluate()`** (section 3).
- Research-only rules may be *stricter*, never looser [PROPOSED]:
  - at most 2 filled entries per coin per session;
  - at most 4 filled entries per session in total;
  - no entry while any simulated order is unresolved (9.3);
  - simulated capital of 10,000 USDT (comparable with earlier results) and 100 USDT (the live cap's
    size, with Binance's 5 USDT minimum order). The 100 USDT run reports how many plans could not be
    placed.

### 9.2 Maximum exposure, loss and concurrency (as enforced by the engine, unchanged)

| Limit | Value |
| --- | --- |
| Open positions | at most 3 (R-04) |
| Correlation group | at most 2 per group (R-08; BTC and ETH are one group) |
| Invested | at most 50% (R-06); one coin at most 25% (R-07) |
| Open stop risk | at most 1.5% (R-05); 0.25% per trade (R-01) |
| Loss limits | daily 1.5% (R-02); weekly 3% (R-03); drawdown breaker 6% (R-09) |

Backtests report both modes already in use:
- *as deployed*: the breaker halts entries;
- *edge measurement*: a breach is recorded and the peak reset.

### 9.3 Unresolved state blocks entries

- Any `UNKNOWN` order, any position without both a confirmed stop and a confirmed target, or any
  difference between the simulator's position record and its fill record **blocks all new entries**.
- Only simulated reconciliation clears it (7.7), and the time it took is recorded.

### 9.4 Missing or inconsistent protection

Every candle, the simulator checks the following for each position:
- the stop and target exist;
- both are sells;
- both cover the held quantity (less one step);
- they are at the planned prices.

Any failure flattens the position at the next open (with market-exit costs), blocks entries and is
counted as a protection fault. In Experiment B the count must be zero. In Experiment C it is injected on
purpose.

### 9.5 Isolation and the fail-safe stop

- Architecture tests (Gate D):
  - the IC modules import nothing from `bot.execution`, `bot.exchange.trading` or any credential loader;
  - they read no `.env`;
  - they open no network socket during a run;
  - they never write into `~/live`, `~/testnet`, `~/paper` or `~/forward-h2b`.
- A run reads only local data files written at Gate C, and writes only into its own folder (for example
  `~/research-ic`).
- **Fail-safe stop:** the run stops, keeps its partial outputs marked INCOMPLETE and records why, on:
  - any data-quality failure;
  - any locked-window candle;
  - memory above a set limit (measured first, as for H4);
  - any invariant violation;
  - any unreadable state.

  `pkill -f "bot research ic"` stops it with no effect on any service. Nothing about a stopped run is
  retried automatically.

---

## 10. Experimental design

### 10.1 Experiment A: H2b reference (no rerun)

- **Reference values [FACT, recorded]:**
  - H2b walk-forward: 104 out-of-sample trades, +1.90 USDT per trade with doubled costs (23 Oct 2026);
  - forward test on new prices running (simulated money; at least 12 months and 30 trades);
  - trading live with a capped amount.

  H2 for context: 121 walk-forward trades, then failed the holdout (27 trades, −2.16 USDT per trade).
- **Use:** a reference point in the report only. **The figures are not comparable like for like:**
  different periods, holding times, coins and cost treatment. No claim of "better than H2b" is
  allowed from Experiment B. The report puts the recorded H2b figures beside IC's with that warning.
- **Not done:** H2b is not rerun, replayed or read from its account.
- [DECISION 9, later] A future overlap study (do IC and H2b lose on the same days?) would need H2b's daily
  results on the same period. That is a separate request, after Gate F, and would not touch H2b's
  files or forward test.

### 10.2 Experiment B: intraday conditional-order research

**Periods [DECISION 1; proposed values]:**

| Part | Period | Use |
| --- | --- | --- |
| Warm-up | 2019-01-01 to 2019-12-31 | Indicators only; no trades |
| Development | 2020-01-01 to 2022-12-31 | Each candidate's settings chosen here (10.3); free exploration *within the pre-registered candidates only* |
| Validation | 2023-01-01 to 2024-09-30 | Each candidate run **once** with its development-chosen settings; no re-tuning |
| Excluded | 2024-10-01 to 2025-10-01 | The intraday plan's locked window: never loaded |
| Excluded | 2025-10-01 to 2026-10-01 | The spent holdout: never loaded |
| **Test (untouched)** | Prospective: from the first 00:00 UTC after Gate A approval, until **at least 6 months and at least 30 test-period trades for the candidate**, at most 12 months [PROPOSED] | Recorded as new candles arrive and **never looked at** until the end. Replayed offline **once**, for all qualified candidates together. A second opening is VOID |

Why prospective:
- Every historical window after 2024-10 is locked or spent.
- Validation (2023 to 2024) was also part of H1 to H4's walk-forward test windows, so it is weakly
  contaminated (I have seen other strategies' results there, never these candidates').
- New prices are the only test nobody has seen. The cost is time: a test result cannot exist before
  about six months after Gate A.

**Leakage rules:**
1. The rules, candidates, settings grid, costs and pass rules are frozen at Gate A, before any 15m data
   for ETH, XRP or BNB is downloaded. BTC 15m was downloaded in intraday step 1. Whether its files were
   inspected beyond the step-1 memory measurement must be stated at Gate A [VERIFY].
2. Indicators see finished candles only; plans activate at the next candle (7.2).
3. Point-in-time liquidity and regime labels (the shared `bot.analysis.regime` rule, never forked).
4. Validation and test files are loaded only by the run command for that part, enforced by a lock
   like `bot.backtest.periods`.
5. No statistic from validation or test feeds back into a setting, filter or coin choice. Any such change
   is a new experiment.
6. Code and data hashes are recorded with every run. A result whose hash differs from the frozen
   version is not counted.

**Parameter selection (development only) [PROPOSED]:**
- Walk-forward inside development: train on 18 months, evaluate on the next 6 (2021-07 to 2022-12, three
  windows).
- The chosen configuration is the one with the best *median* adverse-cost expectancy across those
  windows, **provided its neighbours are also positive**. "Neighbours" means one step away in each setting.
- If no configuration has positive median expectancy in development, the candidate stops there
  (REJECTED at development; it never reaches validation).

### 10.3 Trial log

Every configuration evaluated, in any period, is appended to an immutable trial log: candidate, settings,
period, cost scenario, code hash, data hash, time, and the summary result. The total number of trials is
reported, and the multiple-comparison control (10.4) uses it. Deleting or re-running a trial without
logging it makes the experiment VOID.

### 10.4 Multiple comparisons [PROPOSED]

- **Family size:** at least 6 candidates plus their 108 configurations. Prior ideas on the same history
  (H1, H2, H3, H2b, H4) are counted in the report as context.
- **Validation decision:** Holm–Bonferroni across the 6 candidates at a family-wise α = 0.05 (one-sided).
  Each candidate's p-value comes from a stationary block bootstrap of its daily net results under the
  adverse scenario (H0: mean ≤ 0; mean block length 5 days).
- **Selection bias across configurations:** a reality-check style bootstrap (White's Reality Check or
  Hansen's SPA) over all logged configurations of the candidate, against the random-entry benchmark.
- **The random-entry bar stays at the 99th percentile**, as in the intraday plan.
- A candidate that fails any of these is not "almost qualified". It is REJECTED or INCONCLUSIVE by
  section 13.

### 10.5 Minimum evidence (summary; full rules in section 12.5)

At least 100 validation trades per candidate, and at least 30 in the test period [PROPOSED]. Below these
counts the result is INCONCLUSIVE, never a pass.

### 10.6 Experiment C: execution reliability (fault injection)

C is judged on correctness only, separately from any trading result.

**Fault matrix [PROPOSED]:** each scenario is a test with a stated expected behaviour. Many are
property-based, with at least 3,000 examples, as the repo's convention requires.

| # | Injected fault | Expected behaviour |
| --- | --- | --- |
| C-1 | Order acknowledgement lost (outcome unknown) | Never re-sent; all entries blocked; settled only by reconciliation; "never executed" only when every record agrees |
| C-2 | Delayed acknowledgement (k candles) | No second order meanwhile; state settles correctly when it arrives |
| C-3 | Partial fill, then expiry | Protection sized on the filled quantity; remainder cancelled; below-minimum remainder sold |
| C-4 | Duplicate fill or order-update messages | Idempotent: the position counted once |
| C-5 | Out-of-order messages (fill before acknowledgement) | Final state the same as the in-order case |
| C-6 | Malformed data (missing field, wrong type, NaN, negative quantity, non-boolean side flag) | Treated as a data error: nothing applied, state preserved, entries paused, an alert event written; no crash |
| C-7 | Process restart at every step boundary (before and after the plan, the fill and the protection) | Restored state equal to the pre-crash state; entries blocked until reconciled; no duplicate order |
| C-8 | Failed cancellation (cancel refused, or the order filled meanwhile) | The result is looked up, never assumed; a filled order gets protection or is flattened |
| C-9 | Protection missing or mismatched after a fill | Flattened at the next open; entries blocked; counted |
| C-10 | Exchange outage (no data for n candles) | No triggers or fills during it; stops gapped over fill at the first open after it |
| C-11 | Stop and target both triggered in one candle | Stop taken; never both |
| C-12 | Unreadable or partly written state file | Run stops; file never reset |
| C-13 | Clock skew or data older than allowed | Entries refused (R-12, R-13) |

**Pass bar [PROPOSED]:**
- every scenario passes;
- zero unsafe states, meaning: a position without protection beyond one candle, a duplicate order, an
  entry while state is unresolved, or a state change applied from malformed data;
- every resolution is logged with its time.

One failure blocks Gate E until fixed and re-run.

**Mutation check:** as with the bot's own reviews, planted bugs (gate removed, re-send on timeout, stop
not taken first, malformed data accepted) must each be caught by at least one C test.

---

## 11. Evaluation metrics

Reported per candidate, per baseline, per cost scenario, and per coin, month and regime:

| Metric | Definition / note |
| --- | --- |
| Opportunities | Plans made |
| Entry outcomes | Filled, partial, expired (never reached), gapped through, rejected by the risk engine (by code, especially REWARD_RISK), rejected by exchange rules, blocked (unresolved state, limits) |
| Gross and net result | Total and per trade, in USDT and in R (risk units) |
| Cost breakdown | Fees, spread, slippage, gaps, separately; fees as a share of gross profit |
| Expectancy | Mean net result per trade, with a **95% stationary-bootstrap interval** (blocks by day). Also the 90% interval |
| Distribution | Average win, average loss, median, 5th and 95th percentiles, largest loss, largest win, the share of net profit from the best 5 trades. Win rate is shown but never used alone |
| Maximum drawdown | Daily equity, as deployed and edge-measurement, with a bootstrap interval |
| Exposure | Share of time invested; average and maximum concurrent positions |
| Holding time | Median and distribution; the share ending at target, stop, max-hold and session end |
| Turnover | Traded value per year as a multiple of average equity |
| Sensitivity | Expectancy versus each cost item (fees, spread, slippage, gap model, touch-fill), and each setting ± one step |
| Concentration | Result without the best coin; without the best month; the share from the largest regime |
| Baselines | Random entries with the same exits (distribution, 99th percentile); context-only baseline; cash; buy-and-hold for reference |
| Order-state errors (C, and B where relevant) | Count per type, time to resolution, unresolved count at the end |

**Uncertainty language.**
- Intervals are always shown with the number of trades and days behind them.
- "Statistically significant" is used only for the pre-registered tests in 10.4, at their stated level.
  Otherwise the report says "the interval includes zero" or "excludes zero".
- Profitability alone never qualifies a candidate.

---

## 12. Acceptance criteria (all [PROPOSED]; agreed at Gate A, never changed after)

Each group must pass in full. A threshold below is a proposal, not an established standard.

### 12.1 Data quality

1. Missing 15m candles, excluding exchange outages identified from the data, are at most **0.5%** per
   coin per year.
2. There are zero conflicting duplicates.
3. Rebuilt hours and days match Binance's own 1h and 1d candles in at least **99.9%** of cases. Every
   mismatch is listed.
4. There are zero candles from the locked windows in any development or validation input.
5. Every file checksum matches (where Binance publishes one [VERIFY]).

### 12.2 Execution-model validity

1. No fill outside its candle's range, before activation, or at a better price than the rules allow.
   This is checked by invariant tests on every simulated fill.
2. At least **30 hand-computed golden cases**, covering every row of the tables in 7.4 and 7.6, match
   exactly.
3. The simulator's fills agree with the existing `bot.paper.fills` rules where they overlap.
4. The share of ambiguous candles (7.3) is reported. If results change sign between "with" and "without"
   1m resolution, the candidate is INCONCLUSIVE.

### 12.3 Risk-control correctness

1. Every simulated entry has an accepted `evaluate()` decision; this is checked by tests.
2. There are zero breaches of any limit in 9.2 in any run.
3. There are zero protection faults in Experiment B.
4. Every Experiment C scenario passes (10.6).

### 12.4 Research methodology

1. Rules, candidates, grid, costs and these criteria are frozen at Gate A (the approving commit is the
   record).
2. The trial log is complete; its hash is in every report.
3. Validation runs once per candidate.
4. The test period is opened once, for all candidates together.
5. Code and data hashes match the frozen versions.

### 12.5 Evidence sufficiency (per candidate, adverse costs)

1. At least **100** validation trades and at least **30** test trades.
2. Validation expectancy > 0, and the candidate passes the Holm-adjusted test (10.4).
3. It beats the 99th percentile of random entries with the same exits, and beats its context-only
   baseline.
4. It is still positive without its best coin, and still positive without its best month.
5. Every validation walk-forward window, as deployed, stays below the 6% breaker.
6. The settings one step away are still positive (no knife-edge).
7. Test-period expectancy > 0 under adverse costs. A test result is judged by its sign and interval;
   with 30 or so trades the interval will be wide, and the report says so.

### 12.6 Operational reliability

1. The run completes within the measured memory limit. A memory stop is a failure of the run, not of
   the strategy.
2. Re-running the same frozen inputs reproduces identical outputs (by hash).
3. There are zero unresolved simulated order states at the end of any run.

---

## 13. Failure conditions and stop rules

| Condition | Result |
| --- | --- |
| Data-quality criteria (12.1) not met | Stop before any backtest; Sultan decides (new data source, fewer coins) as a new protocol revision |
| Memory does not fit | Stop; Sultan decides (decision 2); the period is never shortened automatically |
| A look-ahead or leakage bug found before the test period is opened | All results from that code are VOID; fix, re-run development and validation (logged as new trials) |
| The same, after the test period is opened | The whole experiment is VOID; the test period is spent |
| Test period opened early, or twice | VOID |
| A rule, setting, coin or threshold changed after Gate C data is seen | That run is a new experiment, not this one |
| No candidate positive in development | REJECTED (all); no validation run |
| Too few trades (12.5.1) | INCONCLUSIVE; no coin, period or setting added afterwards |
| Results flip sign with 1m resolution, or between base and adverse, near zero | INCONCLUSIVE |
| Fails 12.5.2 to 12.5.6 with enough trades | REJECTED |
| Experiment C failure | Gate E blocked until fixed; Experiment B results produced before the fix are not counted |
| Any IC code found able to reach an order endpoint, credentials or a runner's folder | Stop everything; VOID; report to Sultan |

INCONCLUSIVE is never followed by relaxing a requirement. A later attempt is a new experiment with its
own document and its own unseen test period.

---

## 14. Implementation stages requiring separate approval

Each gate needs Sultan's explicit approval. **Approving one never approves the next.** Live or testnet
trading of any IC candidate, any change to H2b and any change to production settings are outside every
gate.

| Gate | Permits | Does not permit | Delivered for review |
| --- | --- | --- | --- |
| **A: research protocol** | Freezing this document (with Sultan's answers to section 15.2) | Any code, download or run | The approved document; a list of what changed between revisions |
| **B: read-only data-quality tooling** | Code that checks local candle files (section 5.5) and, if decision 7c, a read-only public price recorder; with tests | Downloading research data; any simulator; any account access | PR with tests; architecture test showing no order or credential path |
| **C: historical-data acquisition** | Downloading the 15m (and, if decision 6, 1m) candles for the approved coins and periods only; running the quality checks | Any locked window; any backtest | Quality report (gaps, duplicates, cross-checks, memory measurement) |
| **D: isolated simulator** | The IC simulator, candidates and baselines, with the golden cases and all of Experiment C | Running on development, validation or test data | PR with tests, fault-injection results, mutation check |
| **E: offline backtesting** | Development, then validation runs, as frozen. The test period is replayed once, only after it has accumulated and only on Sultan's command | Changing anything frozen; testnet or live | Trial log, reports per section 11 |
| **F: results review** | Sultan decides whether further research (more simulation) is justified | Trading of any kind, on its own | The final report with its outcome class |

Rollback at any gate: every change adds new files or commands, and no service uses them. Rolling back
is reverting the PR; no account is affected.

---

## 15. Outstanding questions and limitations

### 15.1 Limitations (known now)

- Candle data cannot show the price path inside a candle, queue position or book depth. The
  pessimistic rules reduce, not remove, the risk of fills that could not have happened.
- Historical spreads, past tick and step sizes, and past fee promotions are not available or not used.
  These are assumptions (5.3, 5.4, 8.1).
- Survivorship: four coins that still trade today.
- Validation 2023 to 2024 is weakly contaminated (10.2). The prospective test removes that, at the
  cost of at least six months' wait.
- Thirty-odd test trades give a wide interval. A positive test is weak evidence, which is why the best
  outcome is "qualified for further research", not "approved".
- Stop-slippage allowances are risk settings, not measurements. They probably overstate routine costs
  for BTC, but this has **not been measured** (the same caveat as in the intraday plan).
- The risk engine's reward-to-risk floor (8.4) may reject most intraday plans. That would be a valid
  finding about this idea under today's settings, not a reason to change them.

### 15.2 Decisions that require Sultan's approval

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| 1 | Test period | (a) prospective, new prices after Gate A (6 to 12 months); (b) a historical window | (a): every historical window after 2024-10 is locked or spent |
| 2 | Coins versus memory | (a) compact format (Gate D code, measured first); (b) larger research machine; (c) fewer coins, fixed now; (d) 1h decisions | (a), measured at Gate C before any run; if it fails, stop and decide again |
| 3 | Relation to the paused intraday plan (I1 to I3) | (a) IC is separate, and the intraday plan stays paused and frozen; (b) IC replaces it (a new decision recorded in that plan's history, not an edit to it) | (a); the breakout overlap is counted in 10.4 |
| 4 | Family P versus the rejected H4 | (a) keep P as a new strategy, with H4 reported as prior evidence; (b) drop P | (b), unless Sultan explicitly rules that P is not an H4 re-test |
| 5 | Session definition | (a) UTC day with exit at 23:45 and entry cutoff 20:00; (b) another window | (a): matches the bot's day boundary and daily loss reset |
| 6 | 1m data for ambiguous candles | (a) none (worst path only); (b) 1m only for ambiguous candles | (b), if Gate C shows it fits; results shown both ways |
| 7 | Spread | (a) constant + scenarios; (b) estimators; (c) a read-only recorder for the test period | (a) + (c) |
| 8 | Market exits other than stops | (a) full stop allowance; (b) 0.05% (as the intraday plan's decision 2B), with (a) as sensitivity | (b), with (a) always shown, so results are labelled not comparable with H1 to H4 |
| 9 | Overlap study with H2b | later, separate request after Gate F | defer |
| 10 | All [PROPOSED] thresholds in sections 5 to 12 | approve, change or reject each before Gate A | review each; nothing is fixed until approved |

### 15.3 Facts, assumptions and decisions at a glance

| Item | Fact | Assumption / estimate | Needs approval |
| --- | --- | --- | --- |
| H2b unchanged, forward test running | ✓ | | |
| Holdout 2025-10 to 2026-10 spent; 2024-10 to 2025-10 locked for I1 to I3 | ✓ | | Decision 1, 3 |
| Risk settings and the reward-to-risk formula (incl. 1.5 floor after costs) | ✓ | | never changed |
| Take-profit ≈ +2.9% (BTC) / +3.6% (others) for a 1% stop | ✓ arithmetic from the formula | | |
| 0.1% fee today, BNB discount off | ✓ (today) | same for all history | |
| Candles carry no bid/ask or depth | ✓ | | |
| Historical spreads | | constant or estimated | Decision 7 |
| Entry, stop and market-exit slippage | | settings / estimates | Decision 8 |
| Past tick, step and minimum order = today's | | ✓ | |
| ~940 bytes per candle | ✓ measured | | |
| ~760 MB for four coins at 15m | | ✓ arithmetic estimate | Decision 2 |
| Microsecond timestamps from 2025 on data.binance.vision | | [VERIFY] | |
| Spot bid/ask history not on data.binance.vision | | [VERIFY] | |
| Six candidates, 108 configurations, settings grid | | | Gate A |
| Periods (development, validation, prospective test) | | | Decision 1 |
| Pass thresholds (trade counts, 99th percentile, Holm α = 0.05, 0.5% gaps, 99.9% cross-check, 1% volume cap) | | | Decision 10 |
| Fault matrix and its zero-unsafe-state bar | | | Gate A |
| Gates A to F, each separate | | | Gate A |

### 15.4 Open questions for review

- Is a six-month prospective test acceptable, given that no result can exist sooner?
- Should the 100 USDT budget be the primary simulated capital rather than 10,000? It is the size live
  trading actually uses, and it makes minimum-order rejections visible.
- Should IC wait until the intraday plan's fate (resume or retire) is decided, so the two never compete
  for the same history?
