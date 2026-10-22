# Crypto bot

A risk-first Binance Spot trading bot, built module by module from the Stage 1 design
package. Modules 1 (risk foundation) and 2 (read-only Binance connection) are done:

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

**The bot cannot trade yet.** It reads from Binance but has no code that sends orders.
Order sending comes in a later module, behind this risk engine, and only after testnet
and paper trading.

## Run it

Needs Python 3.11 or newer.

```bash
cd crypto-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                              # 68 tests, about 20 seconds
bot check-config
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000
bot try-trade --equity 10000 --entry 80000 --stop 78000 --target 86000 --loss-today 150
bot check-binance --public-only     # live prices and rules, no key needed
bot check-binance                   # also checks your key is read-only, then reads fees and balances
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
   bash ~/crypto-bot/scripts/make_binance_key.sh
   ```
   It creates the key on the server, shows the public key to paste into Binance
   (API Management > Create API > Self-generated), and stores the API key you
   paste back in `.env`. Give the key **Enable Reading** only and restrict it to
   the server's IP.

## Update the server after new code is pushed

As the `bot` user: `bash ~/crypto-bot/scripts/update_bot.sh` (downloads, installs,
runs the tests, then the read-only Binance check).

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

## Settings you may want to change

All in `config/default.yaml`. You can make limits stricter freely. Looser than the
built-in ceilings is refused at startup on purpose.

The drawdown breaker (6%) does not clear when prices recover: an operator must
complete a review first (`complete_drawdown_review`). If drawdown stays above the
limit, the only way to trade again is a deliberate config change, up to the 10% ceiling.

## Next modules

3. Market data service and paper-trading simulator
4. Backtesting engine and the three candidate strategies
5. Order execution with protection checks and reconciliation
6. Dashboard and Telegram alerts
7. News and AI analysis (advisory only)
