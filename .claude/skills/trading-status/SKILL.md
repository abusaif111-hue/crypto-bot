---
name: trading-status
description: Report how Sultan's trading bot is doing on this server (live account, H2b forward test, paper, testnet, H2b's buy signals), and pause or resume an account when Sultan asks. Use when he asks about his trades, balance, positions, profit, the bot's status, or wants an account stopped or restarted.
---

# Trading status (on the bot server)

You run on Sultan's DigitalOcean server as the `bot` user, in `~/crypto-bot`. Sultan reads your answers on
his phone and is not a programmer: answer in short, plain sentences, with numbers in USDT.

## Monitoring (read-only, allowed without asking)

Run these and report what they say. Never guess or fill in a number that is not in the output.

```bash
live status          # REAL money: H2b, capped account
forward status       # H2b forward test, simulated money (the real test of H2b)
paper status         # paper trading, simulated money
testnet status       # Binance testnet, test money
.venv/bin/bot signals    # how close H2b is to buying each coin
```

For recent activity or an error: `live log 50` (also `forward log`, `paper log`, `testnet log`, `alerts log`).

Report, for each account: running or blocked (and why), equity against what it started with, open trades
(coin, entry, stop, target), closed trades and their total, and anything unusual (a breaker, a mismatch, a
runner that went quiet). Put the live account first. Then one line per coin from `signals`. If nothing has
traded, say so plainly: H2b decides once a day at 00:00 UTC (04:00 Muscat) and can go weeks without a trade.

## Control (only when Sultan asks, in this conversation)

- **Block** an account: `live block --operator Sultan --reason "<his reason>"` (same for forward, paper,
  testnet). Blocking only stops new entries; open trades keep their stop and target (on Binance for live
  and testnet). If Sultan
  asks to stop the bot, block first and say what stays open.
- **Resume** an account: `live resume --operator Sultan --reason "<his reason>"`. Before resuming, show the
  status and the reason it was blocked, and resume only after Sultan confirms. Never resume on your own.

## Never

- Never read, print or edit `.env`, API keys, the Telegram token or the live confirmation phrase.
- Never create or edit `config/live.yaml`, change the live switch, the capital cap or any risk setting, and
  never run `live start`, `live baseline`, `forward start` or `testnet start`. Those are Sultan's own steps,
  typed by him in the console (CLAUDE.md, rules 2 and 5).
- There is no command to buy or sell by hand, and you must not try to make one: every order comes from the
  runner and goes through the risk engine. If Sultan wants a trade closed now, explain that, block new
  entries if he wants, and tell him he can close it himself in the Binance app. Then reconciliation sees
  the difference and blocks new entries until he resumes; that is expected, not a fault.
- Never restart services (`systemctl` needs root): tell Sultan the exact command to run as root.
