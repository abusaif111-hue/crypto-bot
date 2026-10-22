#!/usr/bin/env bash
# Run paper trading around the clock as a system service, restarted after a crash or reboot.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_paper_service.sh
#
# Paper trading reads live prices from Binance's public market data (no API key)
# and moves only simulated money. The service runs as the "bot" user and can
# write only to its paper account folder.
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-paper.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi
STATE_DIR="$BOT_HOME/paper"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 700 "$STATE_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Crypto bot paper trading (simulated money only)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot --config $BOT_HOME/crypto-bot/config/default.yaml paper run --state-dir $STATE_DIR
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now bot-paper
sleep 3
systemctl --no-pager --lines=5 status bot-paper || true
cat <<EOF

Paper trading is running as the service "bot-paper" (simulated money only).
New entries start BLOCKED. To allow them (as the bot user):
    su - $BOT_USER
    cd ~/crypto-bot && . .venv/bin/activate
    bot paper resume --operator YOUR_NAME --reason "start paper trading"
    bot paper status
Service log (as root):   journalctl -u bot-paper -f
Stop / start (as root):  systemctl stop bot-paper  /  systemctl start bot-paper
EOF
