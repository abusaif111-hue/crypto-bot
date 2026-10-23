#!/usr/bin/env bash
# Run the H2b forward test around the clock as a system service, restarted after a crash or reboot.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_forward_service.sh
# The first time (no forward test yet) it only installs the "forward" command and says how to
# start the test; run it again after "forward start" to install and start the service.
#
# The forward test reads live prices from Binance's public market data (no API key) and moves
# only simulated money, in its own account (~/forward-h2b), separate from paper trading.
# The service runs as the "bot" user and can write only to the forward-test folder. It refuses
# to run if H2b's code or the config changed since the start (docs/h2b-plan.md).
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-forward.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi
STATE_DIR="$BOT_HOME/forward-h2b"
install -m 755 "$BOT_HOME/crypto-bot/scripts/forward" /usr/local/bin/forward
if [[ ! -f "$STATE_DIR/forward.json" ]]; then
  cat <<MSG_EOF
The "forward" command is installed. No forward test has been started yet.
Sultan starts it once (it chooses H2b's settings on 2023-10 to 2025-09; about 10-30 minutes):
    systemctl stop bot-paper        # frees memory while the settings are chosen
    nohup forward start --operator Sultan --confirm "I am starting the H2b forward test" > /home/bot/forward-start.log 2>&1 &
    tail -f /home/bot/forward-start.log     # wait for "Started by", then press Ctrl+C
    systemctl start bot-paper
    paper resume --operator Sultan --reason "restart after forward start"
Then run this script again to start the forward-test service.
MSG_EOF
  exit 0
fi

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Crypto bot H2b forward test (simulated money only)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot --config $BOT_HOME/crypto-bot/config/default.yaml forward run --state-dir $STATE_DIR
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
systemctl enable --now bot-forward
sleep 3
systemctl --no-pager --lines=5 status bot-forward || true
cat <<MSG_EOF

The H2b forward test is running as the service "bot-forward" (simulated money only).
New entries start BLOCKED. To allow them, type (as root is fine):
    forward resume --operator Sultan --reason "start the H2b forward test"
    forward status
Service log:             forward log
Stop / start (as root):  systemctl stop bot-forward  /  systemctl start bot-forward
MSG_EOF
