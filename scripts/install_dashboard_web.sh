#!/usr/bin/env bash
# Put the private dashboard on the web behind a password and HTTPS (Sultan's request, 28 Oct 2026).
#
# Run as root:   bash /home/bot/crypto-bot/scripts/install_dashboard_web.sh
#
# Before running it: give the server a name (for example a free one from duckdns.org) that points at
# this server's IP address. The script asks for that name and for a password (typed hidden, twice),
# installs Caddy, and makes Caddy ask for the username and password before it passes anything on to the
# dashboard. Caddy gets the HTTPS certificate itself (Let's Encrypt), so the password travels encrypted.
#
# The dashboard itself does not change: it still listens on 127.0.0.1:8050 only and is read-only; only
# Caddy is on the internet (ports 80 and 443). Turn the web link off at any time: systemctl stop caddy
# The password and its hash are never printed; the hash is kept only in /etc/caddy/Caddyfile (root only).
set -euo pipefail

CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"    # tests point it elsewhere
USERNAME="${DASHBOARD_USER:-sultan}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (the browser console logs you in as root)." >&2
  exit 1
fi
if ! systemctl is-active --quiet bot-dashboard; then
  echo "The dashboard is not running. First run: bash /home/bot/crypto-bot/scripts/install_dashboard_service.sh" >&2
  exit 1
fi

read -r -p "The server's name (for example sultanbot.duckdns.org): " DOMAIN
DOMAIN="${DOMAIN,,}"
if [[ ! "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then
  echo "\"$DOMAIN\" is not a host name like sultanbot.duckdns.org." >&2
  exit 1
fi

# this Droplet's public address, from DigitalOcean's own metadata service (never an outside site)
MY_IP="$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)"
NAME_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}' || true)"
if [[ -z "$NAME_IP" ]]; then
  echo "$DOMAIN does not point anywhere yet. Set its IP address to this server's ($MY_IP) and try again." >&2
  exit 1
fi
if [[ -n "$MY_IP" && "$NAME_IP" != "$MY_IP" ]]; then
  echo "$DOMAIN points at $NAME_IP, but this server is $MY_IP. Fix the name's IP address and try again." >&2
  exit 1
fi

echo "==> Installing Caddy"
apt-get update -qq
apt-get install -y -qq caddy >/dev/null

echo "==> Choose the dashboard password (at least 12 characters; not one you use anywhere else)."
while true; do
  read -r -s -p "Password: " PW1; echo
  read -r -s -p "Again: " PW2; echo
  if [[ "$PW1" != "$PW2" ]]; then
    echo "They do not match; try again."
  elif (( ${#PW1} < 12 )); then
    echo "Too short: use at least 12 characters."
  else
    break
  fi
done
# through stdin, never on the command line (where other processes could see it)
HASH="$(printf '%s\n' "$PW1" | caddy hash-password 2>/dev/null | grep -m1 '^\$2a\$' || true)"
unset PW1 PW2
if [[ "$HASH" != \$2a\$* ]]; then
  echo "Caddy wants the password typed to it directly: type the same password again (twice)."
  HASH="$(caddy hash-password </dev/tty | grep -m1 '^\$2a\$' || true)"
fi
if [[ "$HASH" != \$2a\$* ]]; then
  echo "Caddy did not make a password hash; nothing was changed." >&2
  exit 1
fi

if [[ -f "$CADDYFILE" ]]; then
  cp "$CADDYFILE" "$CADDYFILE.before-dashboard"
fi
umask 077
cat > "$CADDYFILE" <<CADDY_EOF
# The crypto bot's dashboard, behind a password (scripts/install_dashboard_web.sh)
$DOMAIN {
	basicauth {
		$USERNAME $HASH
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}
	reverse_proxy 127.0.0.1:8050
}
CADDY_EOF
chown root:caddy "$CADDYFILE"
chmod 640 "$CADDYFILE"
unset HASH

caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "==> Opening ports 80 and 443 in the firewall (needed for HTTPS)"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

systemctl enable --now caddy >/dev/null 2>&1
systemctl restart caddy
sleep 5
if systemctl is-active --quiet caddy; then
  echo
  echo "Done. Open https://$DOMAIN"
  echo "Username: $USERNAME   Password: the one you just chose."
  echo "The first visit can take a minute while the HTTPS certificate is made."
  echo "If DigitalOcean has a cloud firewall on this Droplet, allow HTTP (80) and HTTPS (443) there too."
  echo "Turn the web link off at any time: systemctl stop caddy"
else
  echo "Caddy did not start; check: journalctl -u caddy -n 30" >&2
  exit 1
fi
