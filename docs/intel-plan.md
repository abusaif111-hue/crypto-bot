# Market intelligence, risk monitor and daily report: upgrade plan

Status: **APPROVED by Sultan on 24 Oct 2026 (decisions in section 10).** Nothing is built yet,
and nothing in this plan changes how the bot trades. It answers Sultan's "Upgrade Specification:
Crypto Market Intelligence and Risk Monitoring Bot".

Every number in section 1 comes from the code or from real server runs. Prices, news and results are
never made up: where this plan shows a report, the values are placeholders like `<value>`.

## 1. What exists today (architecture assessment)

### Modules (Python 3.11+, about 10,400 lines, 576 tests)

| Area | Module | What it does |
| --- | --- | --- |
| Core | `bot.core` | Config with hard ceilings (`CEILINGS`, `FLOORS`), `Decimal` money, IDs |
| Risk engine | `bot.risk.engine.evaluate()` | Every entry is checked here: R-01..R-14 (0.25% risk per trade, 1.5% daily and 3% weekly loss limits, at most 3 positions, 1.5% open risk, 50% invested, 25% per coin, correlation groups, 6% drawdown breaker, reward-to-risk, spread, volume, depth, stale data, clock skew) |
| Breakers | `bot.risk.modes` | Daily, weekly and drawdown blocks; the drawdown block clears only after a review |
| Accounting | `bot.portfolio.accounting` | Equity, drawdown, P&L |
| Binance, read-only | `bot.exchange.binance` | Official `binance-sdk-spot`; GET only, 11 allow-listed endpoints (public market data, account, commission, filters, key restrictions); refuses a key that can trade, withdraw or use margin |
| Binance, trading | `bot.exchange.trading` | 4 order endpoints (order, cancel, OPOCO list, cancel list), testnet or live; live needs `LiveAuthorization` (config switch AND typed phrase in `.env`); the trading key must not withdraw, transfer or use margin |
| Market data | `bot.market.data` | Finished candles only, paging, any error raises and blocks entries |
| Simulator | `bot.paper` | Same risk engine, fees and pessimistic fills; paper account and runner |
| Execution | `bot.execution` | Ledger (PENDING before sending), protected entries (OPOCO), exits that never sell twice, reconciliation (read-only; any difference blocks entries), the testnet/live runner |
| Strategies | `bot.strategies` | H1, H2, H3, H2b, H4 and random baselines; pure, finished candles only, at most 3 settings |
| Research | `bot.backtest` | History download and checks, replay through the simulator, walk-forward (8 windows), the 7-check selection rule, robustness, regimes, random-entry comparison, locked holdout, forward test and its report |
| Monitoring | `bot.alerts`, `bot.dashboard`, `bot signals` | Telegram alerts from the runners' journals (silence detection, daily check), a private read-only web page, H2b's daily checks per coin |

### Storage (no database)

JSON state files and JSONL journals per account (`~/live`, `~/forward-h2b`, `~/paper`, `~/testnet`), CSV
price history (`~/backtest-data`), Markdown and JSON reports (`~/backtests`). Writes are atomic
(temporary file, then rename). An unreadable state file stops a runner and is never reset.

### Services on DigitalOcean (512 MB, 1 vCPU, Frankfurt; systemd, `Restart=on-failure`)

`bot-live` (REAL money, H2b, capped at 300 USDT), `bot-forward` (H2b forward test, simulated),
`bot-paper`, `bot-testnet`, `bot-alerts`, `bot-dashboard`. There are no scheduled jobs (timers) yet.
Claude Code also runs there in tmux for Remote Control. Updates go through `scripts/update_bot.sh`
(git pull, install, the full test suite, a read-only Binance check). After a restart, every runner
comes back with new entries blocked until an operator resumes it.

### API keys (three, separate, all IP-restricted)

| Key | Allowed | Checked by |
| --- | --- | --- |
| Live read-only | Reading only | `bot check-binance` refuses it if it can trade, withdraw or use margin |
| Live trading | Spot trading only | `trading_key_problems` refuses withdrawals, transfers, margin and futures |
| Testnet | Testnet trading | Separate environment |

Keys and the Telegram token live in `.env` (mode 600, never in git; CI refuses a committed `.env`).

### Strategy research so far (real server runs)

| Strategy | Result |
| --- | --- |
| H1, H3 | Did not qualify (walk-forward) |
| H2 | Passed walk-forward, **failed the one-time holdout** (23 Oct 2026); the holdout year is spent |
| H2b | Passed walk-forward (104 trades, +1.90 USDT per trade with doubled costs); its forward test on new prices is running; **trading live** with a 300 USDT cap (Sultan's decision) |
| H4 | Did not qualify (24 Oct 2026): 331 trades, −1.51 USDT per trade |

## 2. What the specification asks for, against what exists

| Requirement | Today | Gap |
| --- | --- | --- |
| Market direction, trend, volume, volatility | Indicators and regime labels inside backtests; `bot signals` for H2b | No daily summary across coins; **no market breadth** |
| Liquidity | Spread, 24h volume and depth checks in the risk engine (per trade) | Not reported daily |
| Macro, central banks, regulation, ETF, exchange incidents, token news | **None** (module 7, News + LLM, was never started) | All of it |
| News sentiment, reliability, duplicates, "already priced in" | None | All of it; see the limits in section 9 |
| Research engine with fees, slippage, drawdown, win rate, expectancy, out-of-sample, sensitivity, regimes, weaknesses | **Exists** (`bot.backtest`; train/test split by walk-forward, holdout, forward test) | A single record of every hypothesis tried, so the number of tries is visible |
| Look-ahead, survivorship, leakage | Look-ahead tested end to end; holdout locked; survivorship bias documented | Nothing new to build |
| Exposure, drawdown, losses | Inside each runner, per account | **No combined view across accounts** |
| Stale prices, abnormal spreads, outages, inconsistent balances | Risk engine (R-11, R-12), market data errors, reconciliation, alert silence detection | Not written down as one daily data-quality and API-health section |
| Concentration, repeated signals, abnormal volatility | Per-coin and correlation limits (R-07, R-08); H2b's volatility pause | No warnings for repeated signals or market-wide volatility |
| Safe default when data or controls fail | **Exists**: every runner fails closed | Keep it so |
| Daily report with a conclusion | None | All of it |
| Structured logs, health checks, alerts | JSONL journals, alert silence and daily checks | A health record for each new job |
| Least privilege, no withdrawals, secrets outside git | **Exists** | Keep it so; new data sources need no keys in phase 1 |

So most of the "research engine" and "risk" parts exist and stay. The new work is the **market
intelligence** (market observations and news), a **combined read-only risk monitor**, and the
**daily report**.

## 3. Prioritized improvements

1. **Risk monitor (read-only)**: one combined view of exposure, drawdown, losses, data quality and API
   health across all accounts, with a reason and the underlying data recorded for every warning.
   Highest value: it watches the account that uses real money.
2. **Market observations from Binance public data**: direction, trend, volatility, volume, spread and
   market breadth for the main coins.
3. **Daily report**: market, risk, research evidence, data quality and a conclusion, sent to Telegram
   and shown on the dashboard.
4. **News from official sources**: central banks, US data releases, regulators and Binance's own
   status, with source, time, coins concerned and a confidence tier; duplicates merged.
5. **Research log**: every hypothesis tried (H1 to H4 so far), its test period, sample size and verdict.
6. **Optional, later**: summaries by a language model, and paid news sources. Both are Sultan's decision
   (cost and licences; section 10).

## 4. Module design and data flow

New modules, each separate and small. **None of them can trade.** Like `bot.alerts` and
`bot.dashboard` today, they import nothing from `bot.execution`, `bot.exchange.trading` or the risk
engine's internals (CLAUDE.md rule 7), and a test enforces this.

| Module | Job | Reads | Writes |
| --- | --- | --- | --- |
| `bot.news.sources` | Fetch official feeds (RSS/Atom/JSON) over HTTPS with timeouts | The internet | Nothing (returns raw items) |
| `bot.news.normalize` | One record per item: source, tier, title, link, published time (UTC), fetched time, coins concerned, category; merge duplicates (same link, or same source and near-identical title) | Raw items | Nothing |
| `bot.intel.market` | Direction, trend, volatility, volume, spread, breadth from finished candles | Binance public data (existing read-only adapter) | Nothing |
| `bot.monitor` | Risk and data-quality warnings, each with a reason, the data behind it and a level | The runners' state files and journals, market observations | Nothing |
| `bot.daily_report` | Assemble the report, decide the conclusion by fixed rules (section 6) | The four modules above, backtest and forward-test reports | `~/reports/<date>.md` and `.json` |
| Delivery | Send a short version by Telegram (the existing sender) and show the full one on the dashboard | The report files | Telegram message |

```
 official feeds ──► news.sources ──► news.normalize ──┐
 Binance public ──► intel.market ─────────────────────┤
 runners' files ──► monitor ──────────────────────────┼──► daily_report ──► ~/reports ──► Telegram, dashboard
 backtest/forward reports ────────────────────────────┘
        (no arrow goes back to the runners: the report cannot place, change or block an order)
```

The report runs **once a day as a short job** (a systemd timer, for example at 00:20 UTC, after the
daily close and H2b's decision), not as a service that stays in memory. The server has 512 MB and
was at 76% use with Claude Code running.

## 5. Data sources (phase 1: free, official, no API key)

Every source below must be checked before it is used: that the address works, the terms allow
automated reading, and the times it gives are reliable. "Verify" means not yet checked.

| Topic | Source | Status |
| --- | --- | --- |
| Prices, volume, spread, breadth | Binance public market data (already used) | In use |
| Binance system status and maintenance | Binance `/sapi/v1/system/status` (a read endpoint; it would be added to `READ_ENDPOINTS`) | Verify |
| Binance listings, delistings, incidents | Binance's official announcements | Verify that an official machine-readable feed exists; if not, not covered |
| US central bank | Federal Reserve press releases (official RSS) | Verify |
| US data releases (CPI, jobs) | Official release calendars (BLS) | Verify |
| European central bank | ECB press releases (official RSS) | Verify |
| US regulation | SEC and CFTC press releases (official RSS) | Verify (the SEC asks for a contact in the request header) |
| ETF flows | No free official source known | **Not covered in phase 1**; the report says so |
| General crypto news, sentiment | Licensed aggregators or publishers | **Not in phase 1**: licence and cost are Sultan's decision |

Source tiers (fixed, used for confidence): **A** official body or exchange announcement, **B**
established news organisation, **C** aggregator or unverified. Phase 1 uses tier A only.

## 6. The daily report

Sections, in order (as the specification asks):

1. **Market regime and uncertainty**: per coin, trend (close against the 50-day average), 30-day
   volatility against its own past year, and the share of the main coins in an uptrend (breadth). The
   regime labels are the same fixed rules the backtests use.
2. **Verified news**: tier-A items from the last 24 hours with source, link, time, coins concerned,
   category and confidence. No prediction of impact: "possible implications" are fixed text per
   category (for example, a rate decision: "can move all risk assets; volatility often rises around the
   announcement"). Items already older than the last daily close are marked "before the last close"
   (possibly priced in).
3. **Major-asset observations**: BTC, ETH and the forward test's coins: daily change, distance from
   the 50-day average, volume against 30-day average, spread.
4. **Scenarios and invalidation**: written from H2b's own rules, not forecasts. For example: "H2b buys
   ETH if 2 of its 3 returns clear their bars; today 1 of 3; this is invalidated if the daily close falls
   below `<value>`."
5. **Risk**: per account, exposure, open risk, drawdown against the 6% breaker, losses today and this
   week against their limits, and every warning from the monitor.
6. **Research evidence**: the research log: each strategy, test period, number of trades and verdict;
   the forward test's days and trades so far against its required length.
7. **Data quality and API health**: price age, missing candles, Binance status, each feed's last success,
   failed fetches, and the clock check.
8. **Conclusion**, by fixed rules, never forced positive:
   - **Pause analysis**: critical data is missing or stale, a runner has gone quiet, or reconciliation
     failed.
   - **Insufficient evidence**: the default, and the answer for any strategy without a passed
     walk-forward and a finished forward test.
   - **Sufficient evidence for further simulation**: only for a strategy that passed walk-forward and
     whose forward test is running without a breach. H2b today.

Format sketch (placeholders only):

```
Daily report, <date> 00:20 UTC. Simulated research and monitoring; not a forecast or advice.
Conclusion: <Insufficient evidence | Sufficient evidence for further simulation (H2b) | Pause analysis>

Market: BTC <trend>, 30-day volatility <value> (<percentile> of its year); breadth <n> of <m> coins in an uptrend.
News (tier A, last 24 h): <time UTC> <source>: <title> (<coins>, <category>, confidence <tier>) <link>
Risk: live <equity> USDT of <capital> cap, open risk <value>%, drawdown <value>% (breaker 6%); warnings: <list or none>
Evidence: H2b walk-forward passed (2022-01 to 2025-09, 104 trades); forward test day <n>, <k> trades of at least 30.
Data: prices <age> s old; feeds <ok>/<total>; Binance status <value>.
Limits: <list>
```

Deliverable 6 (an example report) will be produced by running the code on **past dates from the
research period**, clearly labelled as historical, once the code exists. Not before.

## 7. Risk monitor rules

Each warning records: time, level (info, warning, critical), rule, reason in words, and the numbers
it used. Phase 1 rules (all thresholds fixed in code and pinned by tests):

| Rule | Level | When |
| --- | --- | --- |
| Stale prices | critical | Latest finished candle or book price older than its interval plus a margin |
| Abnormal spread | warning | Spread above the risk engine's own limit (R-11, 0.10%) |
| Data outage | critical | A feed or Binance call failed on its last try, or a runner's journal is silent |
| Inconsistent balances | critical | The last reconciliation of the live or testnet account found a difference |
| Drawdown | warning at half the breaker, critical at the breaker | Per account |
| Daily or weekly loss | warning at half the limit | Per account |
| Concentration | warning | One coin or one correlation group above its limit share of open risk |
| Repeated signals | warning | The same coin proposed and refused more than 3 times in 3 days |
| Abnormal volatility | warning | A coin's 30-day volatility in the top 10% of its past year, or a daily move beyond 3 times that volatility |
| Insufficient evidence | info | Fewer observations than the method needs (for example, the forward test's minimum) |

The monitor never claims protection: the report says plainly that stops can be gapped through, can fill
worse than their price, and depend on Binance being up.

**Safe default.** The runners already stop new entries by themselves on stale data, errors and failed
reconciliation; that stays exactly as it is. Whether the monitor may also *block* new entries on a
critical warning is decision 5 below. Blocking only makes the bot stricter, but it is a new path into
the runners.

## 8. Implementation plan (small, reviewable pull requests, each with tests)

| # | Change | Touches trading code? |
| --- | --- | --- |
| 1 | `bot.monitor` + `bot monitor` command (read-only) | No |
| 2 | `bot.intel.market` (observations, breadth) | No |
| 3 | `bot.daily_report` + `bot report` command + systemd timer + Telegram and dashboard delivery | No |
| 4 | Research log (`docs/research-log.md`, generated from saved reports) | No |
| 5 | `bot.news.sources` + `bot.news.normalize` (tier-A feeds after each is verified) | No |
| 6 | Binance system status endpoint added to `READ_ENDPOINTS` (a read endpoint) | Read-only adapter only |
| 7 | Optional: language-model summaries, paid sources (only if Sultan approves) | No |

Tests the specification asks for, in every relevant module: stale data, missing fields, duplicated
events, API failures (timeouts, errors, malformed answers), and restarts (the job runs twice, or stops
half-way: no duplicate Telegram message, no half-written report). Plus the existing architecture tests,
extended: the new modules cannot import anything that trades.

During all of this the bot's trading code is not changed. Tests never trade: they use made-up data and
fake exchanges, as today.

## 9. Known limits (stated now, so the report never overstates)

- **News impact cannot be measured reliably.** The report shows what was announced, by whom and when;
  it does not predict price moves or give probabilities.
- "Already priced in" can only be shown roughly: the item's time against the last daily close and the
  move since.
- Phase 1 covers official sources only, so it misses rumours, social media and most ETF data. The report
  lists what is not covered.
- Market breadth uses a fixed list of large coins chosen before any data is looked at; coins that were
  delisted are missing (survivorship bias).
- Feeds can be late, changed or withdrawn; a failed feed is shown as such, never filled in.
- The daily report is research and monitoring. It is not a forecast or advice, and it never changes a
  trade.
- On the 512 MB server, memory is tight. The report runs as a short job, but a resize to 1 GB would
  remove the risk.

## 10. Sultan's decisions (24 Oct 2026)

Sultan chose decisions 1, 2 and 6 himself; for 3, 4, 5 and 7 he accepted the recommended answers.

- [x] 1. **Live trading during this work:** (a) keep it running. Nothing in this work changes it.
- [x] 2. **"100 USDT a day":** (a) lower the live capital from 300 to 100 USDT. Sultan does this himself
      with the steps he was given (a fresh live account record with 100 USDT, only with nothing open).
- [x] 3. **News sources:** (a) official sources only (phase 1, free, no keys).
- [x] 4. **Language-model summaries:** later (not in this work).
- [x] 5. **Monitor on a critical warning:** (b) also block new entries on the affected account, through
      the runner's existing block request. Open trades keep their stop and target.
- [x] 6. **Server size:** keep 512 MB. The report stays a short daily job.
- [x] 7. The plan in sections 4 to 8 is approved.

## 11. Deployment and rollback (DigitalOcean)

Deploy (each pull request, after Sultan merges it), as the bot user:

```bash
bash ~/crypto-bot/scripts/update_bot.sh        # pulls, installs, runs every test, read-only Binance check
```

Then, once only, as root, for the daily report timer (in pull request 3):

```bash
bash /home/bot/crypto-bot/scripts/install_report_timer.sh
```

Existing services need no restart for these changes: they do not use the new modules.

Rollback:

1. Stop the new job (as root): `systemctl disable --now bot-report.timer`.
2. Go back to the previous version (as the bot user): `cd ~/crypto-bot && git checkout <previous tag>`
   then `.venv/bin/pip install -e .`. Each deploy gets a tag first (`git tag before-intel-<n>`).
3. The trading services keep running throughout; the new modules never write to their folders, so
   rolling back cannot affect an account.

## 12. Security checklist

- [x] Live read-only key: reading only, IP-restricted (checked by `bot check-binance` at every update).
- [x] Live trading key: Spot only; no withdrawals, transfers, margin or futures (`trading_key_problems`).
- [x] Testnet key separate.
- [x] Keys and token only in `.env` (mode 600, gitignored; CI refuses a committed `.env`).
- [x] The live switch needs both the config and the typed phrase; neither is in git (a test checks).
- [x] No code path can withdraw or transfer (architecture test).
- [x] Claude on the server: denied `.env`, the config files and Sultan's start steps (`.claude/settings.json`).
- [ ] Phase 1 news needs no new keys. If an aggregator or language model is added, its key goes in `.env`
      and its errors are scrubbed like the Telegram token's.
- [ ] Rotate the three Binance keys and the Telegram token if the server or `.env` is ever exposed.
- [ ] GitHub: keep the repository private; turn on secret scanning and push protection
      (Settings → Code security), so a key pushed by mistake is blocked.
- [ ] No account identifiers in git: reports and journals stay on the server (`~/reports`, `~/live`).
- [ ] Server: SSH keys instead of the root password (offered earlier, still open).
