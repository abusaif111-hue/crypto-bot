#!/usr/bin/env bash
# Run the LIVE runner (REAL MONEY) around the clock as a system service. Sultan only.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_live_service.sh
# It installs the "live" command, and starts the service only when the live account exists
# (bot live start, which itself needs Sultan's live switch). The runner refuses to trade unless the
# live config has environment: live and live_trading: true AND BOT_LIVE_CONFIRM in .env holds the
# exact phrase, and the live trading key can trade Spot and nothing else.
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-live.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi
STATE_DIR="$BOT_HOME/live"
LIVE_CONFIG="$BOT_HOME/crypto-bot/config/live.yaml"
install -m 755 "$BOT_HOME/crypto-bot/scripts/live" /usr/local/bin/live
if [[ ! -f "$LIVE_CONFIG" || ! -f "$STATE_DIR/account.json" ]]; then
  cat <<MSG_EOF
The "live" command is installed. The live account has not been started yet (see README, "Going live").
Then run this script again to start the live service.
MSG_EOF
  exit 0
fi

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Crypto bot LIVE runner (Binance Spot, REAL MONEY, capped)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot --config $LIVE_CONFIG live run --state-dir $STATE_DIR
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
systemctl enable --now bot-live
sleep 3
systemctl --no-pager --lines=5 status bot-live || true
cat <<MSG_EOF

The LIVE runner is running as the service "bot-live" (REAL money, capped).
New entries start BLOCKED. To allow them, type (as root is fine):
    live resume --operator Sultan --reason "..."
    live status
Block new entries:       live block --operator Sultan --reason "..."
Service log:             live log
Stop / start (as root):  systemctl stop bot-live  /  systemctl start bot-live
Open trades keep their stop-loss and take-profit on Binance while the service is stopped.
MSG_EOF
