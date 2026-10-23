#!/usr/bin/env bash
# Run the testnet runner around the clock as a system service, restarted after a crash or reboot.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_testnet_service.sh
# The first time (no runner account yet) it only installs the "testnet" command and says how to
# start the account; run it again after "testnet start" to install and start the service.
#
# The runner places real orders on Binance's SPOT TESTNET (fake money) with the testnet key, using
# H2b with the forward test's frozen settings and coins. Signals come from Binance's public live
# market data (no key). The service runs as the "bot" user and can write only to ~/testnet.
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-testnet.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi
STATE_DIR="$BOT_HOME/testnet"
install -m 755 "$BOT_HOME/crypto-bot/scripts/testnet" /usr/local/bin/testnet
if [[ ! -f "$STATE_DIR/account.json" ]]; then
  cat <<MSG_EOF
The "testnet" command is installed. The runner's account has not been started yet:
    testnet start --operator Sultan --capital 1000
Then run this script again to start the service.
MSG_EOF
  exit 0
fi

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Crypto bot testnet runner (Binance SPOT TESTNET, fake money)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot --config $BOT_HOME/crypto-bot/config/default.yaml testnet run --state-dir $STATE_DIR
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now bot-testnet
sleep 3
systemctl --no-pager --lines=5 status bot-testnet || true
cat <<MSG_EOF

The testnet runner is running as the service "bot-testnet" (fake money, testnet only).
New entries start BLOCKED. To allow them, type (as root is fine):
    testnet resume --operator Sultan --reason "start the testnet runner"
    testnet status
Service log:             testnet log
Stop / start (as root):  systemctl stop bot-testnet  /  systemctl start bot-testnet
MSG_EOF
