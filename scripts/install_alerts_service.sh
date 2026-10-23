#!/usr/bin/env bash
# Run the Telegram alert service around the clock, restarted after a crash or reboot.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_alerts_service.sh
# First, as the bot user, connect Telegram once:   cd ~/crypto-bot && .venv/bin/bot alerts setup
#
# The service reads the journals of the testnet runner, paper trading and the H2b forward test and
# sends what matters to Telegram. It only reads those files and sends messages; it cannot trade.
# It runs as the "bot" user and can write only to ~/alerts (where it keeps its place).
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-alerts.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi
install -m 755 "$BOT_HOME/crypto-bot/scripts/alerts" /usr/local/bin/alerts
if ! grep -q '^TELEGRAM_CHAT_ID=-\?[0-9]' "$BOT_HOME/crypto-bot/.env" 2>/dev/null; then
  cat <<MSG_EOF
The "alerts" command is installed. Telegram is not connected yet. As the bot user:
    su - bot
    cd ~/crypto-bot && .venv/bin/bot alerts setup
Then run this script again (as root) to start the alert service.
MSG_EOF
  exit 0
fi
STATE_DIR="$BOT_HOME/alerts"
install -d -m 700 -o "$BOT_USER" -g "$BOT_USER" "$STATE_DIR"

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Crypto bot Telegram alerts (reads the runners' journals; cannot trade)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot alerts run --home $BOT_HOME --state-dir $STATE_DIR
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
systemctl enable --now bot-alerts
sleep 3
systemctl --no-pager --lines=5 status bot-alerts || true
cat <<MSG_EOF

The alert service is running as "bot-alerts". You should get a "watching ..." message on Telegram now.
Service log:             alerts log
Send a test message:     alerts test
Stop / start (as root):  systemctl stop bot-alerts  /  systemctl start bot-alerts
MSG_EOF
