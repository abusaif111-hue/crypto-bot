#!/usr/bin/env bash
# Run the private dashboard around the clock as a system service. Read-only; this machine only.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_dashboard_service.sh
#
# The dashboard reads the runners' files and shows every account on one page. It listens on
# 127.0.0.1:8050 only, so it is never on the internet: open it from your own computer through an
# SSH tunnel (see README, "Dashboard"). The service runs as the "bot" user and cannot write anywhere.
set -euo pipefail

BOT_USER=bot
UNIT=/etc/systemd/system/bot-dashboard.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Crypto bot private dashboard (read-only, 127.0.0.1:8050)
After=network-online.target

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot dashboard --home $BOT_HOME --host 127.0.0.1 --port 8050
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
MemoryMax=120M

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now bot-dashboard
sleep 3
systemctl --no-pager --lines=3 status bot-dashboard || true
if curl -fsS http://127.0.0.1:8050/health >/dev/null 2>&1; then
  echo
  echo "The dashboard is running on this server at 127.0.0.1:8050 (read-only, not on the internet)."
  echo "Open it from your own computer through an SSH tunnel: see README, \"Dashboard\"."
else
  echo "The dashboard did not answer yet; check: journalctl -u bot-dashboard -n 20" >&2
fi
