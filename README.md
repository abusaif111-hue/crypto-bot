# Crypto bot

A risk-first Binance Spot trading bot, built module by module from the Stage 1 design
package. Modules 1 (risk foundation), 2 (read-only Binance connection) and 3 (market data
and paper-trading simulator) are done:

| Part | Folder | What it does |
| --- | --- | --- |
| Config with hard ceilings | `src/bot/core/config.py`, `config/default.yaml` | Loads settings; refuses any risk limit looser than its built-in ceiling; keeps live trading off unless explicitly confirmed |
| Exchange rules | `src/bot/exchange/filters.py` | Reads Binance symbol filters (tick size, step size, minimum order value, price bands, MAX_ASSET) and checks orders against them |
| Risk engine | `src/bot/risk/engine.py` | Sizes or rejects every proposed trade against rules R-01 to R-14 |
| Circuit breakers and modes | `src/bot/risk/modes.py` | Daily, weekly and drawdown breakers; RUNNING / ENTRIES_BLOCKED / PAUSED / EMERGENCY_FLATTEN |
| Accounting | `src/bot/portfolio/accounting.py` | Equity at best bid, daily and weekly P&L without deposits and withdrawals, drawdown index |
| Client order IDs | `src/bot/core/ids.py` | Deterministic IDs so a retried order can never be placed twice |
| Binance connection, read-only | `src/bot/exchange/binance.py` | Reads prices, exchange rules, fees, balances and the API key's own permissions through Binance's official SDK; can only send GET requests to a fixed list of read endpoints |
| Credentials | `src/bot/exchange/credentials.py` | Loads the API key and key file path from `.env`; refuses files other users can read; never prints the key |
| Venue interface | `src/bot/exchange/interface.py` | The read methods every venue offers; the real adapter and the simulator both implement it |
| Market data service | `src/bot/market/data.py` | Builds the price, spread, volume, depth, data-age and clock-skew snapshot the risk engine needs; any failure blocks new entries |
| Paper-trading simulator | `src/bot/paper/sim.py`, `src/bot/paper/fills.py` | Simulated exchange with 10,000 USDT of simulated money; every entry goes through the risk engine; fills pay the 0.1% taker fee and slippage; exits by simulated stop-loss, take-profit and time stop |
| Paper-trading runner | `src/bot/paper/runner.py`, `src/bot/paper/store.py` | `bot paper run` checks live prices every 5 seconds, fills simulated exits, logs every decision and trade, and saves the account after every check so restarts lose nothing |
| Backtesting, steps 1-2 of 3 | `src/bot/backtest/`, `src/bot/analysis/`, `src/bot/strategies/` | Downloads and checks price history, replays it through the paper-trading simulator and the risk engine, reports the baselines, and tests strategies H1-H3 with walk-forward and robustness checks against the selection rule. The last 12 months are locked away for the final test |

**The bot cannot trade yet.** It reads from Binance but has no code that sends orders.
The paper-trading simulator only moves simulated money inside the program.
Order sending comes in a later module, behind this risk engine, and only after testnet
and paper trading.

## Run it

Needs Python 3.11 or newer.

```bash
cd crypto-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                              # 285 tests, about 70 seconds
bot check-config
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 --loss-today 150
bot check-binance --public-only     # live prices and rules, no key needed
bot check-binance                   # also checks your key is read-only, then reads fees and balances

bot paper run                       # paper trading on live prices, simulated money (Ctrl+C stops it)
bot paper status                    # balances, positions, trades, progress toward the paper review
bot paper resume --operator Sultan --reason "start paper trading"
bot paper trade --operator Sultan --reason "test idea: breakout above range" \
    --entry 80000 --stop 78000 --target 86000 --hours 72
bot paper block --operator Sultan --reason "pause new entries for news"
```

`check-binance` stops with exit code 3 if the API key can trade, withdraw or is not
IP-restricted. Balances show as one total; add `--details` to list each asset.

`try-trade` runs a trade idea through the real risk engine using example market
data, and prints whether it is accepted, the size, the worst planned loss and why.

## Put it on a server (browser only, nothing installed on your laptop)

1. Rent a small Ubuntu 24.04 server in a region where Binance service is permitted
   (for example Frankfurt; not the US). Open its browser console; you are root.
2. Download the code and run the setup script:
   ```bash
   apt-get update && apt-get -y install git
   git clone https://github.com/abusaif111-hue/crypto-bot.git
   bash crypto-bot/scripts/setup_server.sh
   ```
   `git clone` asks for your GitHub username and, as password, a fine-grained
   token limited to this repository with Contents: Read-only. The script
   installs everything, runs the tests and prints the server's IP address.
3. Connect Binance **read-only** (only useful once module 2 is installed):
   ```bash
   su - bot
   bash ~/crypto-bot/scripts/make_binance_key.sh --live-read-only
   ```
   It creates the key on the server, shows the public key to paste into Binance
   (API Management > Create API > Self-generated), and stores the API key you
   paste back in `.env`. Give the key **Enable Reading** only and restrict it to
   the server's IP.

4. Start paper trading as a service that runs all the time and restarts after a crash or
   reboot (as root):
   ```bash
   bash /home/bot/crypto-bot/scripts/install_paper_service.sh
   ```
   It needs no API key: paper trading reads Binance's public prices and moves only
   simulated money. It also installs a short `paper` command you can type as root.
   New entries start blocked; allow them and check on it with:
   ```bash
   paper resume --operator Sultan --reason "start paper trading"
   paper status
   paper trade --operator Sultan --reason "test idea" --entry 82000 --stop 80000 --target 87000 --hours 72
   paper log
   ```
   `paper` always runs as the `bot` user. The full record of every decision and trade
   is `/home/bot/paper/journal.jsonl`.

## Update the server after new code is pushed

As the `bot` user: `bash ~/crypto-bot/scripts/update_bot.sh` (downloads, installs,
runs the tests, then the read-only Binance check). If paper trading runs as a service,
restart it as root afterwards (`systemctl restart bot-paper`) and resume entries again
(`paper resume ...`). To pick up a new version of the `paper` command itself, run
`install_paper_service.sh` again; it keeps the paper account.

## Backtesting (steps 1 and 2 of 3)

On the server, as the `bot` user (`su - bot`, then `cd ~/crypto-bot`):

```bash
bot backtest fetch                  # 4-hour BTC/ETH candles since 2019, public data, no API key
bot backtest baselines              # cash, buy-and-hold BTC, 50/50 BTC/ETH, 200 random-entry runs
```

`fetch` stores the candles in `~/backtest-data` and refuses impossible prices; gaps
(Binance maintenance) are listed. `baselines` writes `report.md` and `results.json` to
`~/backtests/<date>-baselines/` and prints the report.

- **Periods (fixed in `src/bot/backtest/periods.py`):** warm-up from 2019-01-01 (indicators
  only), research 2020-01-01 to 2025-10-01, and a locked holdout 2025-10-01 to 2026-10-01.
  No backtest code can read a holdout candle; only the holdout command (step 3, run by
  Sultan) will.
- **Same code as paper trading:** history is replayed through `SimExchange`, so every
  entry goes through the risk engine with the same fee, slippage and exits. An idea
  made at a candle's close fills at the next candle's open; strategies only see
  finished candles.
- **Costs:** 0.1% taker fee, 0.01% half-spread, the risk config's slippage (0.05% entry,
  0.5% BTC / 1.0% ETH stops); a stop passed in a gap fills at the next open. Doubled
  costs double all of these.
- **Breakers in a backtest:** daily and weekly blocks resume once cleared (as if Sultan
  resumed after review); the drawdown breaker stops trading for the rest of the run.
- **Random entries** use the same exits and sizing as a strategy: stop 2 x ATR(14), target
  3 x the stop distance, 96-hour time stop. A strategy that cannot beat them has no edge.
- **Every report** records the git commit, a config fingerprint, the data range, the cost
  model and the limits of simulation.

### Step 2: strategies H1-H3

```bash
nohup bot backtest run > ~/backtests/run.log 2>&1 &     # about an hour; keeps going if the console closes
tail -f ~/backtests/run.log                             # watch progress (Ctrl+C stops watching, not the run)
```

| Strategy | Idea (design package) | Tunable settings (defaults) |
| --- | --- | --- |
| H1 | 4h breakout above the recent range high while the daily close is above its 50-day average; ATR stop trailed behind the highest close; time stop | range candles (30), ATR multiple (2.5), horizon hours (168) |
| H2 | Daily time-series momentum: hold while the multi-week return beats a volatility-scaled threshold; sell when it turns negative | lookback weeks (4), threshold (0.5), ATR multiple (3) |
| H3 | Buy oversold 4h dips (RSI) only when the daily average is flat (range regime); sell when RSI is back above 50; short time stop | oversold level (30), regime days (20), time stop hours (24) |

- **Walk-forward:** 8 test windows of 6 months (2022-01 to 2025-09). For each, the settings
  are chosen on the 24 months before it from 27 combinations (each setting at 80/100/120%),
  by net profit per unit of drawdown with at least 20 trades; then tested once on the unseen
  window.
- **Two modes (Sultan's decision):** *as deployed*, the 6% drawdown breaker stops trading;
  *edge measurement* records each breach, resets the peak and keeps measuring. The reset
  exists only in backtests.
- **Selection rule** (all must pass on walk-forward, then the holdout): positive expectancy
  after doubled costs; every test window below the 6% breaker as deployed; at least 100
  out-of-sample trades; not driven by one coin or one month; no collapse when a setting
  moves 20%; better than random entries with the same exits (95th percentile).
- Reports also break results down by coin and by market regime at entry (bull, bear,
  sideways, high volatility; rules fixed before testing).
- The paper runner does not yet apply a strategy's own exits (trailing stop, exit signals);
  that is added if a strategy passes, before it paper trades.

### Re-testing H2 on more coins (approved by Sultan)

First results (Oct 2026): none of H1-H3 qualified on BTC and ETH. H2 failed only the
100-trade check (70 trades). H2 is re-tested **unchanged** on a wider universe chosen by
rules fixed before any new coin was looked at (`src/bot/backtest/universe.py`):

```bash
bot backtest screen                 # checks every Binance USDT pair; writes ~/backtest-data/screen/
bot backtest fetch --screened       # 4h history of the picked coins
nohup bot backtest run --strategy H2 --screened > ~/backtests/run-h2-wide.log 2>&1 &
```

- **Candidates:** USDT spot pairs trading today with daily candles since 2019-10-01 or
  earlier ("no new listings", D-16); no stablecoins, fiat, gold, wrapped or leveraged tokens.
- **Screen**, over 2022-10-01 to 2025-10-01 (before the holdout, never touching it):
  30-day volatility above 2x BTC's on at most 25% of days; worst fall at most 70%; at least
  20 million USDT traded on 95% of days. Up to 4 passing coins with the most volume (D-16
  allows 8; 4 fits the 512 MB server, about 14 MB per coin). `bot backtest run` refuses to
  start if free memory is too low.
- **Correlation groups (R-08 as written):** correlation with BTC 0.8 or more joins BTC/ETH in
  "majors"; below that a coin gets its own group. R-04 (3 positions) and R-05 (1.5% open
  risk) still cap the total.
- Backtests only: paper and live trading keep BTC and ETH until Sultan decides otherwise.
- Survivorship bias: delisted coins cannot be screened.

Result (23 Oct 2026): the screen picked XRP and BNB; H2 on BTC, ETH, XRP and BNB **passed
the walk-forward part** of the selection rule (121 trades, +1.05 USDT per trade after
doubled costs).

### H4: hourly pullbacks in an uptrend (`docs/h4-plan.md`, approved by Sultan)

H4 checks every hour: in a coin whose daily trend is up (close above its 50-day average, and
that average rising over 10 days), it buys the first hour of a sharp drop (the close more than
`dip` standard deviations below the last 24 hourly closes). Stop `stop_atr` x daily ATR(14),
take-profit 3R, time stop 72 hours, and a sale when the daily close falls below its 50-day
average. Two tunable settings (`dip` 2.0, `stop_atr` 1.0). It never runs on the holdout.

```bash
bot backtest fetch --interval 1h --screened             # 1h history of BTC, ETH, XRP and BNB (public data)
bot backtest h4-memory --operator Sultan                # once: four coins, or BTC and ETH only
nohup bot backtest run --strategy H4 > ~/backtests/run-h4.log 2>&1 &
tail -f ~/backtests/run-h4.log
```

- **Which coins (Sultan's decision 4a):** `h4-memory` measures, in a fresh process, the peak
  memory of one full research-period H4 backtest with BTC alone and with BTC and ETH, and
  estimates four coins from the difference. Four coins are used only if that estimate plus
  30 MB fits in the memory available at the start (with the services running). The H4 results
  of those runs are thrown away unseen. The answer is written once to
  `~/backtest-data/h4-coins.json` and never overwritten; `run --strategy H4` uses only those coins.
- H4 runs on its own (`--strategy H4`), on 1h candles. Its random-entry comparison uses the same
  1h candles, H4's own exits and the same number of random ideas per day as for the 4h strategies.
- Same walk-forward and the same 7-check selection rule. If H4 fails any check it is dropped,
  not tuned. If it passes, the next step is its own forward test with simulated money
  (`~/forward-h4`), which only Sultan starts.

Result (24 Oct 2026, report `~/backtests/20261024-111331-strategy-H4`): the memory check chose BTC and
ETH only; **H4 does not qualify** and is rejected as its plan says (not tuned, no forward test).
Out of sample: 331 trades, −500.97 USDT, −1.51 USDT per trade, −5.68 with doubled costs. Its entries did
no better than random entries with the same exits (−2.32 USDT per trade against a random median of
−2.17). Fees over the whole research period: 465.52 USDT on 536 trades.

### Step 3: the holdout (Sultan only, once)

```bash
bot backtest holdout --strategy H2 --screened --operator Sultan \
    --confirm "I am running the one-time holdout test"
```

Rules fixed before any holdout result (approved by Sultan):
- H2's settings are chosen by the walk-forward method on the 24 months before the holdout
  (2023-10 to 2025-09, research data only); same coins and correlation groups.
- The holdout year (2025-10-01 to 2026-10-01) must show positive expectancy per trade after
  doubled costs, and the 6% drawdown breaker must never trip as deployed.
- The 100-trade rule and the one-coin / one-month checks use walk-forward and holdout trades
  together.
- Before anything is opened, the command checks the confirmation phrase, that the data reaches
  2026-10-01, that a passing walk-forward report exists for this config and these coins, and
  free memory; a mistake never uses up the holdout.
- Every opening is recorded in `~/backtest-data/holdout-log.json`. The holdout can be used once
  in total: after the first opening, every later run, for any strategy, is marked VOID and
  cannot qualify (tightened after H2 used it; approved by Sultan). A failure rejects the
  strategy: no adjusting and re-running.

Result (23 Oct 2026): **H2 failed the holdout** (27 trades, −2.16 USDT per trade, −4.05 with
doubled costs) and is rejected. The holdout year is spent; no strategy can be judged on it again.

### After the holdout: H2b (plan approved by Sultan)

H2b is a new strategy, defined and frozen in [`docs/h2b-plan.md`](docs/h2b-plan.md):
the 14-, 28- and 56-day returns must agree (2 of 3), with no new entries while a coin's
30-day volatility is in the top fifth of its past year. Only 2 tunable settings. It never
runs on the holdout (`bot backtest holdout` refuses it).

Step B of the plan, the sanity check on the research period (same walk-forward, same 7 checks).
It takes longer than H2: about 1.5 to 2 times as long on the same server.

```bash
bot backtest run --strategy H2b --screened
```

If H2b fails any check, it is dropped; nothing is tuned and re-run. If it passes, the real
test is the forward test on new prices (later modules).

Result (23 Oct 2026, run on the server): H2b **passed all 7 checks** on BTC, ETH, XRP and BNB:
104 out-of-sample trades (Jan 2022 to Sep 2025), +392.23 USDT, +3.77 USDT per trade, +1.90 with
doubled costs; 3 of the 8 test windows lost money. This data has been used many times, so the
pass is necessary but proves little; the forward test decides.

### The H2b forward test (Sultan starts it, once)

H2b runs on live prices with 10,000 USDT of simulated money in its **own account**
(`~/forward-h2b`, service `bot-forward`), separate from paper trading. It does not count
toward the Stage 6 paper review. Rules from the plan (`docs/h2b-plan.md`, section 3):

- **Fixed at the start**, in `~/forward-h2b/forward.json`: H2b's settings, chosen by the
  walk-forward method on 2023-10 to 2025-09 (research data only; the spent holdout year is not
  used); the coins BTC and ETH (one group), XRP and BNB (each alone); fingerprints of H2b's code
  and of the config. If either changes, the service refuses to run: a change to H2b restarts
  the test from zero, in a new folder. A forward test never restarts in place.
- **Only H2b trades**: manual `trade` requests are refused in this account.
- **Length:** at least 12 months and 30 closed trades, at most 24 months. If more than 10% of
  the time is missed (runner down or failing), both dates move by the time missed.
- **Early end only for failure:** if the 6% drawdown breaker trips, H2b fails at once.
- **Judged once, at the end**, by `forward report` (refused while the test runs): positive
  expectancy after doubled costs, the breaker never tripped, 30+ trades, not driven by one coin
  or one month, and better than random entries with the same exits on the same forward prices
  (95th percentile). The verdict is saved in `~/forward-h2b/verdict.json` and is final; asking
  again only shows it. For information it also shows cash, buy-and-hold BTC and a replay of H2b.
  The random-entry replays use the 13 months before the start (part of the spent holdout year)
  only as indicator history, as the live runner does; nothing before the start is traded or measured.

On the server, as root:

```bash
bash /home/bot/crypto-bot/scripts/install_forward_service.sh   # first time: installs the "forward" command
systemctl stop bot-paper                                        # frees memory while settings are chosen
nohup forward start --operator Sultan --confirm "I am starting the H2b forward test" > /home/bot/forward-start.log 2>&1 &
tail -f /home/bot/forward-start.log                             # wait for "Started by", then Ctrl+C
systemctl start bot-paper
paper resume --operator Sultan --reason "restart after forward start"
bash /home/bot/crypto-bot/scripts/install_forward_service.sh   # second time: starts the service
forward resume --operator Sultan --reason "start the H2b forward test"
forward status
```

When `forward status` says READY TO JUDGE (or ENDED, or FAILED):

```bash
systemctl stop bot-forward             # the account must no longer change
systemctl stop bot-paper               # memory for the replays; start it again afterwards
forward report --operator Sultan
```

## Order execution on the testnet (module 5, in progress)

Real orders are built step by step on Binance's **Spot testnet** (testnet.binance.vision, fake
money). Step 5a is the trading adapter (`bot.exchange.trading`), separate from the read-only
adapter, which stays read-only:

- It may call only 7 endpoints (place, test, query and cancel an order; open orders; account;
  own trades), and refuses any environment but the testnet.
- **Never two orders for one idea.** Binance accepts a repeated client order ID once the first
  order has filled, so a blind retry could buy twice. When an order's outcome is unknown
  (timeout, network error, Binance's "execution status unknown"), the adapter never re-sends it:
  it looks the order up by its ID, and if Binance still cannot say, it reports "outcome unknown"
  so new entries stay blocked until reconciliation (step 5c) settles it.
- Orders are checked against Binance's rules before sending (price and size steps, minimum
  order value). An entry is always a limit order with a price cap; a market order is allowed
  only to sell (an exit).
- A separate testnet key (`BINANCE_TESTNET_*` in `.env`), never the live read-only key.

Set it up on the server, as the bot user:

```bash
bash ~/crypto-bot/scripts/make_binance_key.sh --testnet   # create the testnet key, follow the steps
cd ~/crypto-bot && .venv/bin/bot check-trading            # checks the key and sends a TEST order only
```

`check-trading` places nothing: Binance validates a test order without trading.

**Protected entries (5b, `bot.execution.entry`).** The only way the bot opens a position on an
exchange. `submit()` runs the risk engine itself, then sends one Binance OPOCO order list:

- a **fill-or-kill** BUY at the engine's entry price: it fills completely or not at all, so a
  partly filled buy never waits unprotected;
- once it has filled, a SELL **take-profit** (LIMIT_MAKER) and a SELL **stop-loss** (market
  STOP_LOSS; where Binance offers only STOP_LOSS_LIMIT, its limit sits at the stop minus the coin's
  stop-slippage allowance, the loss the engine already sized for) on the coins actually received.

After the fill, both protective orders are looked up by their own IDs: they must be open, on the
SELL side, at the planned prices and for the coins received. If not, the protection is cancelled,
the coins are sold at market and new entries are blocked. A trade too small for its stop order to
meet Binance's minimum is refused before sending. Verify on testnet: OPOCO with a fill-or-kill buy.

One complete protected trade on the testnet, recorded toward the Stage 5 gate (5 clean round trips):

```bash
cd ~/crypto-bot && .venv/bin/bot testnet-roundtrip                # BTCUSDT, stop 1.5%, sized on 1,000 USDT
.venv/bin/bot testnet-roundtrip --symbol ETHUSDT --hold 30
```

It enters, confirms the protection, holds, cancels the protection, sells, and checks that nothing of
the trade is left open. It prints the fill prices and the P&L before fees (testnet money). Each
attempt goes to `~/testnet/roundtrips.jsonl`; only clean ones count. The Stage 5 gate was met on
23 Oct 2026: 5 clean BTC round trips.

**Reconciliation (5c, `bot.execution.ledger` and `bot.execution.reconcile`).** The bot keeps its
own record of its exchange trades, the ledger (`~/testnet/ledger.json`). A trade is written there
*before* its order is sent, so after a crash or a timeout it can always be looked up by its ID.
Reconciliation compares the ledger with Binance and only reads; it never places or cancels anything:

- a trade whose outcome was not recorded is looked up; not found after 5 minutes means it was never
  placed;
- every open trade must have both protective orders open for the coins held. If one filled, Binance
  closed the trade; if both are gone and no closing sale filled, the coins are **unprotected**;
- every open order on the account must be one of the bot's open trades;
- each watched coin's balance must equal the baseline (coins that are not the bot's) plus the coins
  of open trades, within one step. USDT is not compared: it moves with prices, fees and P&L.

Anything that differs blocks new entries until an operator looks. `testnet-roundtrip` reconciles
before it starts (and refuses if the result is not clean) and again after the close. Once, before
the next round trip, record the baseline (the coins Binance gives every testnet account):

```bash
cd ~/crypto-bot && .venv/bin/bot reconcile --set-baseline --operator Sultan --reason "testnet account as given"
.venv/bin/bot reconcile                   # CLEAN, or what differs; places nothing
```

If Binance resets the testnet account, the balances no longer match: check that no trade is open,
then record a new baseline. Fees paid in BNB move the BNB balance, which also shows as a mismatch.
Verify on testnet: how soon an order that timed out can be looked up (the 5-minute wait).

**The testnet runner (5d, `bot.execution.runner`, service `bot-testnet`).** H2b places real orders on
the testnet by itself, with the forward test's frozen settings and coins. It decides on live prices
(the same ones the forward test sees); its orders, the risk engine's market checks and its valuations
use the testnet's own prices. Every 60 seconds it:

1. reconciles the ledger with the testnet account. Coins found unprotected are sold at market at once.
   A mismatch or unprotected coins block new entries until an operator resumes; an exchange error or
   an order still unsettled pauses entries for that round only;
2. values the bot's account and carries out resume and block requests;
3. sells trades whose time stop (30 days for H2b) has passed;
4. once per finished 4-hour candle: H2b's exits for its own trades, then its new ideas. Each idea is
   written to the ledger first, then goes through the risk engine and is sent as one protected order
   list (fill-or-kill buy, stop-loss, take-profit), as in the round trips.

The bot trades a **capped capital** inside the testnet account (default 1,000 USDT of testnet
money): its equity is that capital plus every change in USDT and the watched coins since the start,
so fees and whatever closed a trade are counted. Daily, weekly and drawdown limits work as everywhere
else; a restart keeps the breakers and blocks entries until an operator resumes.

```bash
cd ~/crypto-bot && .venv/bin/bot testnet start --operator Sultan --capital 1000   # once
# as root:
bash /home/bot/crypto-bot/scripts/install_testnet_service.sh      # installs "testnet", starts bot-testnet
testnet resume --operator Sultan --reason "start the testnet runner"
testnet status
testnet log
```

Once the runner's account exists, `testnet-roundtrip` refuses (its trades would mix with the
runner's), and `bot reconcile` only reports while the runner is running. On the testnet the risk
engine may reject coins whose testnet trading volume is too thin (it did for ETH): that is the
rule working, not a fault. If Binance resets the testnet account, the account's value drops and the
drawdown breaker trips: stop the service, move `account.json` aside, record a new baseline and start
again. Verify on testnet: the first H2b trade end to end, and a time stop or strategy exit.

**Telegram alerts (5e, `bot.alerts`, service `bot-alerts`).** A separate service reads the journals
the runners already write (testnet runner, paper trading, H2b forward test) and sends a short message:

- 🔴 act now: coins possibly unprotected, the account not matching the ledger, a failed exit, an order
  whose outcome is unknown, a breaker, a runner silent for 40 minutes;
- 🟡 worth a look: entries blocked, a runner restarted, errors that repeat, entries paused or market
  data down for over 10 minutes (shorter outages are not reported);
- 🟢 for the record: a trade opened or closed, entries resumed, a fault cleared; and a daily check at
  08:00 Muscat with each runner's mode and equity (no daily check means the alert service is down).

It only reads files and sends messages; it imports nothing that trades. Repeats are held back (15
minutes for 🔴, an hour for 🟡). The bot token is typed hidden, kept in `.env` (mode 600), and never
printed or logged.

Set it up once: in Telegram, open @BotFather, send `/newbot`, pick a name and a username ending in
`bot`, and keep the token it shows (never paste it into a chat). Then on the server:

```bash
su - bot
cd ~/crypto-bot && .venv/bin/bot alerts setup     # paste the token (hidden), then message your bot
exit
bash /home/bot/crypto-bot/scripts/install_alerts_service.sh      # as root: starts bot-alerts
alerts test
```

## Risk monitor (`bot monitor`, read-only)

One check across every account (live, testnet, the H2b forward test, paper) and the coins they trade
(`docs/intel-plan.md`, section 7). Each finding has a level, the rule, the reason and the numbers it used:

| Rule | Level |
| --- | --- |
| A runner silent for 40 minutes, or its last price read failed; Binance unreachable | critical |
| Reconciliation found a difference (live, testnet) | critical |
| Drawdown past half the 6% breaker / at the breaker | warning / critical |
| Daily or weekly loss past half its limit; another breaker active | warning |
| One coin above 25% of the account, or more open trades in a correlation group than allowed | warning |
| The same coin proposed and not traded more than 3 times in 3 days | warning |
| Newest finished 4h candle more than 4h 10min old; server clock off Binance by more than 500 ms | critical |
| Spread above 0.10%; 30-day volatility in the top 10% of its year; a daily move beyond 3x that volatility | warning |
| Too little history or too few trades to judge (for example the forward test's 30 trades and 12 months) | info |

```bash
cd ~/crypto-bot && .venv/bin/bot monitor              # as the bot user; findings are added to ~/monitor/findings.jsonl
```

It only reads: it imports nothing that trades, never writes into a runner's folder, never reads `.env`
and never blocks or resumes anything (the runners still stop new entries on their own). Stops are not a
guarantee: a price can gap through a stop, a stop can fill worse than its price, and stops depend on
Binance being up.

## Market observations (`bot market`, read-only)

For each coin the forward test trades (BTC, ETH, XRP, BNB), from finished daily candles:

- **Price:** the last daily close and its change over 1 and 7 days.
- **Trend:** above or below its 50-day average, by how much, and whether that average is rising.
- **Regime:** the same rule the backtests use (bull, bear or sideways, from the 200-day average and its
  20-day slope).
- **Volatility:** the 30-day volatility, and what share of the past year it is higher than.
- **Volume and spread:** the last day's volume against its 30-day average, and the current spread.
- **Breadth:** how many of the coins are above their 50-day average.

```bash
cd ~/crypto-bot && .venv/bin/bot market               # as the bot user
```

These are observations, not forecasts. Short or missing history is shown as such, never filled in.

## Daily report (`bot report`, read-only)

Once a day after the daily close, one report from what the bot already measures, in the order the plan
asks for: market regime and uncertainty, news (not covered yet, and it says so), the coins'
observations, scenarios from H2b's own rules (how far each coin is from H2b's bars, and the exits of any
open trade), risk per account with every monitor warning, the research record and the forward test's
progress, and data quality. The conclusion follows fixed rules and is never forced positive:

| Conclusion | When |
| --- | --- |
| Pause analysis | Any critical finding: missing or stale prices, a silent runner, a failed reconciliation, a tripped drawdown breaker |
| Sufficient evidence for further simulation (H2b) | H2b's forward test is running without a breach (H2b is the one strategy that passed walk-forward) |
| Insufficient evidence | Otherwise (the default) |

```bash
cd ~/crypto-bot && .venv/bin/bot report               # print and save to ~/reports/<date>.md (as the bot user)
bash /home/bot/crypto-bot/scripts/install_report_timer.sh   # as root, once: every day at 00:20 UTC (04:20 Muscat)
```

The timer runs `bot report --send`: the short version goes to Telegram **at most once a day** (a run that
stopped while sending is not repeated; `--resend` sends again). The dashboard shows the latest report.
It reads only: it cannot trade, never writes into a runner's folder, and reads the Telegram token only to
send. Stop it with `systemctl disable --now bot-report.timer`.

## Official news (`bot news`, read-only)

The daily report's News section lists announcements from official bodies' own feeds (tier A):

| Source | Kept |
| --- | --- |
| Federal Reserve, monetary policy | everything |
| European Central Bank, press releases | interest rate and monetary policy decisions |
| US Bureau of Labor Statistics: CPI, Employment Situation, PPI (one feed each) | everything |
| US SEC, press releases | only items that mention crypto (needs `NEWS_CONTACT`, see below) |
| US CFTC, press releases | only items that mention crypto |

```bash
cd ~/crypto-bot && .venv/bin/bot news check           # once, as the bot user: which sources work on this server
cd ~/crypto-bot && .venv/bin/bot news show            # the last 24 hours
```

- Only sources that passed `bot news check` are used. Every item shows its source, time (UTC), link, the
  coins it names (or "all coins" for a macro item), a category and a confidence from the source's tier.
- An item without a readable time, or dated in the future, is left out and counted, never given a guessed
  time. Duplicates (same link, or same source and title) are merged. Items from before the last daily
  close are marked.
- News is listed, not scored: the report never says what an announcement will do to prices. Not covered:
  ETF flows, exchange incidents, listings and token announcements, general news and social media.
- Requests carry a generic user agent and nothing about you. The SEC refuses requests without a contact
  address; it is used only if you put `NEWS_CONTACT=<an address you choose>` in `.env`.

## Dashboard (private, read-only)

`bot dashboard` (service `bot-dashboard`) shows every account on one page: the live account, the
testnet runner, the H2b forward test and paper trading. For each one it shows the mode and any
breaker, equity against what it started with, drawdown, open trades with their stop and target,
closed trades, and recent activity. It refreshes itself every minute and flags a runner that has gone
silent.

It only reads the runners' files: it imports nothing that trades, never talks to Binance and never
reads `.env`. It answers GET only and listens on `127.0.0.1:8050`, so it is never on the internet; you
open it from your own computer through an SSH tunnel.

```bash
bash /home/bot/crypto-bot/scripts/install_dashboard_service.sh      # as root, once
```

Then on your own computer (Windows PowerShell, macOS or Linux terminal), keep this running:

```bash
ssh -N -L 8050:127.0.0.1:8050 root@167.71.59.188
```

and open **http://localhost:8050** in your browser. Close the terminal (or Ctrl+C) to close the tunnel.
This needs SSH access to the server (your SSH key or root password from DigitalOcean).

## How close is H2b to buying? (`bot signals`, read-only)

`bot signals` shows, for each of the forward test's coins, what H2b checks once a day after the
00:00 UTC daily close: each lookback's return (14, 28 and 56 days) against the bar it must clear
(threshold x 30-day volatility x the square root of the days; H2b buys when 2 of the 3 clear it),
the volatility pause (no entries while today's 30-day volatility is in the past year's top 20%),
and the exit vote for an open trade (sells when 2 of the 3 returns are below zero). The verdict
comes from H2b's own code, so it always matches what the runners do; the risk engine still decides
every trade, and a coin already held is not bought again.

It uses the forward test's frozen settings and public live prices, places no order, writes no file
and changes nothing; a warning is shown if H2b's code or the config differ from the forward test's.

```bash
cd ~/crypto-bot && .venv/bin/bot signals          # as the bot user
```

## Going live (module 5f; REAL money; Sultan only)

The live mode is the testnet runner on the real exchange, with more locks. Nothing is bought or sold on
the real exchange unless **all** of these hold, and only Sultan sets them:

1. a separate **live trading key** (`BINANCE_LIVE_TRADE_*`) that can read and trade Spot and nothing
   else: no withdrawals, no transfers, no margin, futures or options, and locked to the server's IP.
   The bot reads the key's restrictions from Binance and refuses to start if the key can do more;
2. a live config, `config/live.yaml` (never uploaded), that differs from `config/default.yaml` only in
   `environment: live` and `live_trading: true`. The risk settings must stay the forward test's own;
3. `BOT_LIVE_CONFIRM` in `.env` set to the exact phrase in `src/bot/core/config.py`
   (`LIVE_CONFIRM_PHRASE`). Without both 2 and 3, the trading adapter refuses every order endpoint on
   the real exchange (it can still read and send TEST orders, which Binance validates without trading);
4. a live account started with a capped capital, at most `LIVE_CAPITAL_CEILING_USDT` (1,000 USDT, in
   code). The risk engine sizes on the capital at most: profits, or USDT deposited later, never make a
   trade bigger;
5. entries resumed by Sultan (`live resume`).

How much capital a trade needs: the risk engine risks 0.25% per trade and Binance's smallest order is
5 USDT. Checked with the risk engine on Binance's BTCUSDT rules, the smallest capital that gets a protected
BTC order through is 110 USDT with a stop 4% below the entry, 155 at 6%, 225 at 9% and 330 at 12% (H2b's
stop is 3 daily ATRs). With less, every trade is rejected, correctly; the limits are never loosened.

Steps, as the bot user unless marked root:

```bash
bash ~/crypto-bot/scripts/make_binance_key.sh --live-trading     # the live TRADING key; follow the steps
cd ~/crypto-bot && .venv/bin/bot live check                       # key, account, a TEST order; trades nothing
sed 's/^environment: paper/environment: live/' config/default.yaml > config/live.yaml
nano config/live.yaml        # Sultan: change live_trading: false to true himself
nano .env                    # Sultan: set BOT_LIVE_CONFIRM= to the phrase in src/bot/core/config.py
.venv/bin/bot --config config/live.yaml live check                # now says: Live switch .... ON
.venv/bin/bot --config config/live.yaml live baseline --operator Sultan --reason "my own coins, not the bot's"
.venv/bin/bot --config config/live.yaml live start --operator Sultan --capital 300
# as root:
bash /home/bot/crypto-bot/scripts/install_live_service.sh         # starts bot-live; entries blocked
live resume --operator Sultan --reason "..."
live status
```

While the live runner runs, do not trade BTC, ETH, XRP, BNB or move USDT in the same Binance account by
hand: reconciliation would block entries, and a USDT deposit or withdrawal would distort the bot's
equity (a withdrawal can trip the drawdown breaker). To stop at once: `systemctl stop bot-live` (as
root); open trades keep their stop-loss and take-profit on Binance. `live block` stops new entries
only. Removing `BOT_LIVE_CONFIRM` or setting `live_trading: false` stops the runner from starting
again. Telegram alerts cover the live runner too.

## How a trade is sized

1. Entry is assumed to fill slightly worse than planned (0.05%), and the stop
   worse again (0.5% for BTC, 1.0% for ETH and others), with the taker fee on both.
2. That gives the worst loss per coin bought.
3. The risk budget is the smallest of: 0.25% of equity per trade; what is left of
   today's 1.5% loss limit; what is left of the week's 3%; what is left of the
   1.5% total open-risk limit. Losses that could still happen at open
   positions' stops count against these budgets.
4. Quantity = budget ÷ worst loss per coin, then capped by the 50% invested limit,
   the 25% per-coin limit, free cash and order-book depth, and rounded **down**
   to Binance's step size.
5. If the result is below Binance's minimum order, the trade is skipped. Size is
   never rounded up and the risk limit is never raised to make a trade fit.

## How paper trading fills orders

The simulator (`SimExchange`) reads real prices through the read-only adapter but keeps
its own balances, starting at 10,000 USDT. It starts with entries blocked, like the real
bot; `resume(operator, reason)` allows them.

- **Entry:** `submit(intent)` is the only way in. It builds the account state, gets a
  market snapshot and calls the risk engine; a rejected idea changes nothing. An accepted
  one fills immediately in full or not at all, at the ask plus 0.05% slippage. If that
  is above the worst entry price the risk engine sized for, or the ask is already at or
  below the stop, nothing is bought.
- **Exits:** a simulated stop-loss and take-profit for the full quantity, placed as soon
  as the entry fills; when one fills the other is cancelled. The stop sells at the stop
  (or the lower price, if price jumped past it) minus 0.5% for BTC or 1.0% for ETH. The
  take-profit sells at its price. When the trade's holding time runs out, it sells at
  the bid minus the same slippage.
- **Candles:** if one candle touches both the stop and the target, the stop is assumed
  to have filled first. Candles that started before the entry, and unfinished candles,
  are ignored.
- **Fees:** 0.1% taker fee on every buy and sell, charged in USDT. Real Binance takes
  a buy's fee from the coin received unless fees are paid in BNB; module 5 must handle
  that (verify on testnet).
- **Saving:** the runner saves the account to `~/paper/state.json` after every check
  (written to a temporary file first, so a crash mid-save keeps the last good copy).
  A file it cannot read stops the runner; it is never replaced by a fresh account.
- **After a restart** new entries stay blocked until you run `bot paper resume`, and any
  breaker that was active stays active: restarting never clears a breaker. Exits keep
  working while entries are blocked.
- **If prices stop arriving**, entries are rejected and the outage is logged once. Exits
  are checked again as soon as prices return; a stop that was passed meanwhile fills at
  the price seen then and is marked as gapped.
- **Commands while it runs:** `resume`, `block` and `trade` leave a request file that the
  runner picks up within seconds; only the runner changes the account. A manual `trade`
  goes through the risk engine like any other idea. A second runner on the same account
  is refused.
- **Strategies:** the runner asks each strategy once per finished candle of its interval
  (H2b: every 4 hours, deciding once a day), as the backtester does: first its exit check on
  the positions it opened (never your manual trades), then new ideas, which go through the
  risk engine. The last decided candle is saved in `~/paper/strategy-clock.json`, so a restart
  neither repeats nor skips a decision; candles missed while the runner was down are logged
  as `decisions_missed`. A failing strategy is logged once and retried every minute.
  `bot paper run` has no strategy yet: the H2b forward test gets its own account (next module).

## What is tested

- Every rule has a worked example (`tests/unit/test_risk_engine.py`).
- A property-based test generates 3,000 random accounts, positions, prices and
  configs per run (about 700 of them produce accepted trades) and checks that no
  accepted trade breaks any limit. It was also run once at 20,000 cases.
- Circuit breakers: a daily breach holds until the next day; weekly and drawdown
  breaches never clear by themselves; resuming needs an operator name and a reason.
- Config: every value above a hard ceiling is refused; live mode needs both the
  config flag and the typed phrase in `BOT_LIVE_CONFIRM`.
- Architecture: no wallet, withdrawal or transfer code exists; the risk and
  accounting code makes no network calls and cannot import the news or AI modules.
- Paper trading: every simulated entry calls the risk engine (checked by a spy and by
  reading the source); fees, slippage, gaps, take-profit, time stop and same-candle cases
  have worked examples; breakers block entries. A property test runs 400 random trade
  plans and price paths (about 600 closed trades) and checks that balances never go
  negative, every fill pays exactly 0.1%, the cash adds up, and a stop that did not gap
  never loses more than the risk engine planned.
- Paper runner: saving and restoring keep every balance, position, trade and breaker; a
  restart blocks entries and cannot clear a breaker; corrupt files stop the runner;
  requests, price outages, broken strategies, a second runner and a clean stop are covered.
- H2b: every fixed value matches the frozen plan; the 2-of-3 rule, the volatility pause (today
  against the 365 days before, at exactly the 80th percentile), the exits and the history
  requirement have worked examples; future prices never change past decisions; the holdout
  command refuses it. Each of these was checked by planting a bug and seeing the test fail.
- Strategies in the paper runner: asked once per finished candle, a restart neither repeats nor
  skips a decision, outages are logged, failures are logged once and retried every minute, and a
  strategy can only exit its own positions.
- Forward test: started once with the exact phrase and the plan's coins, never restarted or
  overwritten in place; settings chosen on 2023-10 to 2025-09 from research data only; changed
  H2b code or config stops it; no manual trades; the 12/24-month, 30-trade and outage rules and
  the early failure on the drawdown breaker have worked examples.
- Forward report: each of the 6 checks fails on its own example; doubled costs add exactly one
  more round of fees and slippage; only trades closed by the end count; the replay cannot trade
  before the forward start or in the holdout year; it is refused while the test runs, with the
  runner running, or on incomplete prices; the verdict is written once.
- Trading adapter (testnet): an order whose outcome is unknown is looked up, never sent twice;
  bad orders are never sent; only the 7 allow-listed endpoints; testnet only; only the execution
  code and `check-trading` can reach it; `check-trading` sends a test order only.
- Protected entries: the risk engine runs on every submit and nothing is sent when it says no; one
  OPOCO list with a fill-or-kill buy at the engine's prices and size; the fee taken in the coin is
  allowed for; unconfirmed protection (missing, not open, wrong price, type or size) means sell at
  market and block; a failed emergency sale says act now; only `entry.submit` places entries.
- Candle history longer than Binance's 1,000 per request is fetched page by page, and a failed
  page fails closed.

## Settings you may want to change

All in `config/default.yaml`. You can make limits stricter freely. Looser than the
built-in ceilings is refused at startup on purpose.

The drawdown breaker (6%) does not clear when prices recover: an operator must
complete a review first (`complete_drawdown_review`). If drawdown stays above the
limit, the only way to trade again is a deliberate config change, up to the 10% ceiling.

## Next modules

4. H2b (`docs/h2b-plan.md`): the strategy, paged candle history, the stricter holdout lock,
   strategies in the paper runner and the forward test (start, run, status, end-of-test report) are
   done; H2b passed the walk-forward check (23 Oct 2026); the forward test is Sultan's to start
5. Order execution on the testnet: 5a trading adapter, 5b protected entries (gate met: 5 clean
   round trips), 5c reconciliation, 5d the testnet runner, 5e Telegram alerts and 5f the live mode
   (off until Sultan switches it on) done
6. Private dashboard (done) and Telegram alerts (done, 5e); next the loss-review loop
7. News and AI analysis (advisory only)
