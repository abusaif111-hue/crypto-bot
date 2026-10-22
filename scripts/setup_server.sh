#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the crypto bot.
#
# Run as root from inside the cloned repository:
#     bash crypto-bot/scripts/setup_server.sh
#
# It installs Python and a firewall, creates a non-root user "bot", copies the
# code to /home/bot/crypto-bot, installs it and runs the tests. It touches no
# API keys and starts no trading.
set -euo pipefail

BOT_USER=bot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="/home/${BOT_USER}/crypto-bot"

say() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
if ! grep -qi ubuntu /etc/os-release; then
  echo "Warning: written for Ubuntu 24.04; this server is not Ubuntu." >&2
fi
if [[ ! -f "$SRC_DIR/pyproject.toml" ]]; then
  echo "Run this from the cloned crypto-bot repository." >&2
  exit 1
fi

say "Updating the system (this can take a few minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install python3-venv git ufw openssl unattended-upgrades

say "Turning on automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

say "Firewall: block everything incoming except SSH"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

say "Creating the user '${BOT_USER}' (the bot never runs as root)"
if ! id "$BOT_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$BOT_USER"
fi

say "Copying the code to ${DEST_DIR}"
if [[ "$SRC_DIR" != "$DEST_DIR" ]]; then
  if [[ -e "$DEST_DIR" ]]; then
    echo "${DEST_DIR} already exists; leaving it as it is."
  else
    cp -a "$SRC_DIR" "$DEST_DIR"
  fi
fi
chown -R "$BOT_USER:$BOT_USER" "$DEST_DIR"

say "Installing the bot and running its tests"
sudo -u "$BOT_USER" -H bash -c "
  set -euo pipefail
  cd '$DEST_DIR'
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -e '.[dev]'
  pytest
  bot check-config
  mkdir -p -m 700 ~/.secrets
"

IP="$(curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
say "Done"
cat <<EOF
The bot is installed in ${DEST_DIR} and all tests passed.
Live trading is OFF. Nothing is connected to Binance yet.

This server's public IP address (Binance will need it): ${IP}

Next, when you are ready to connect Binance READ-ONLY:
    su - ${BOT_USER}
    bash ~/crypto-bot/scripts/make_binance_key.sh
EOF
