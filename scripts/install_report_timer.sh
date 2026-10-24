#!/usr/bin/env bash
# Send the daily report once a day, at 00:20 UTC (04:20 Muscat), after the daily close and H2b's decision.
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_report_timer.sh
#
# A short job, not a service that stays in memory (the 512 MB server). It runs "bot report --send" as the
# "bot" user: it reads the runners' files and public prices, writes ~/reports and ~/monitor, and sends the
# short version to Telegram at most once a day. It cannot trade. To stop it:
#   systemctl disable --now bot-report.timer
set -euo pipefail

BOT_USER=bot
SERVICE=/etc/systemd/system/bot-report.service
TIMER=/etc/systemd/system/bot-report.timer

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -z "$BOT_HOME" || ! -x "$BOT_HOME/crypto-bot/.venv/bin/bot" ]]; then
  echo "The bot is not installed for user '$BOT_USER'; run scripts/setup_server.sh first." >&2
  exit 1
fi

cat > "$SERVICE" <<UNIT_EOF
[Unit]
Description=Crypto bot daily report (read-only; Telegram once a day)
After=network-online.target

[Service]
Type=oneshot
User=$BOT_USER
WorkingDirectory=$BOT_HOME/crypto-bot
ExecStart=$BOT_HOME/crypto-bot/.venv/bin/bot report --home $BOT_HOME --send
NoNewPrivileges=true
PrivateTmp=true
MemoryMax=200M
TimeoutStartSec=600
UNIT_EOF

cat > "$TIMER" <<UNIT_EOF
[Unit]
Description=Daily report at 00:20 UTC (04:20 Muscat)

[Timer]
OnCalendar=*-*-* 00:20:00 UTC
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now bot-report.timer
systemctl --no-pager list-timers bot-report.timer || true
echo
echo "The daily report will run every day at 00:20 UTC (04:20 Muscat)."
echo "Run it now once:  systemctl start bot-report   then: journalctl -u bot-report -n 40"
echo "Stop it:          systemctl disable --now bot-report.timer"
