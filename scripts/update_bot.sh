#!/usr/bin/env bash
# Update the bot on the server after new code is pushed to GitHub.
# Run as the "bot" user:   bash ~/crypto-bot/scripts/update_bot.sh
# Installs the new code, runs all tests, then the read-only Binance check.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ $EUID -eq 0 ]]; then
  echo "Run this as the bot user:  su - bot" >&2
  exit 1
fi

echo "==> Downloading the latest code"
git pull --ff-only

echo "==> Installing"
[[ -d .venv ]] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e '.[dev]'

echo "==> Running the tests"
pytest

echo "==> Read-only Binance check"
if [[ -f .env ]]; then
  bot check-binance
else
  echo "(no .env yet: checking public market data only; run scripts/make_binance_key.sh to add the key)"
  bot check-binance --public-only
fi

if systemctl is-enabled bot-paper >/dev/null 2>&1; then
  echo
  echo "==> The paper-trading service is still running the old code. To load the new code, as root run:"
  echo "    systemctl restart bot-paper"
  echo "    (after a restart, new entries stay blocked until: bot paper resume --operator NAME --reason \"...\")"
fi
