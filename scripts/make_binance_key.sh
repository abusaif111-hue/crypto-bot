#!/usr/bin/env bash
# Creates the Binance API key pair ON THE SERVER and stores the API key in .env.
#
# Run as the "bot" user:
#     bash ~/crypto-bot/scripts/make_binance_key.sh
#
# The private key is generated here and never leaves this server. You copy only
# the PUBLIC key into Binance (API Management > Create API > Self-generated),
# then paste back the API key Binance shows. Nothing secret is printed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${HOME}/.secrets"
KEY_FILE="${SECRETS_DIR}/binance_ed25519.pem"
ENV_FILE="${REPO_DIR}/.env"

if [[ $EUID -eq 0 ]]; then
  echo "Run this as the bot user, not root:  su - bot" >&2
  exit 1
fi

umask 077
mkdir -p -m 700 "$SECRETS_DIR"

if [[ -f "$KEY_FILE" ]]; then
  echo "A key already exists at $KEY_FILE; keeping it (delete it yourself to start over)."
else
  openssl genpkey -algorithm ed25519 -out "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "New private key created at $KEY_FILE (readable only by this user)."
fi

cat <<'EOF'

------------------------------------------------------------------
STEP 1. Copy the PUBLIC key below (all lines, including BEGIN/END).
It is safe to copy; it cannot be used to trade on its own.
------------------------------------------------------------------
EOF
openssl pkey -in "$KEY_FILE" -pubout
cat <<'EOF'
------------------------------------------------------------------
STEP 2. In Binance: API Management > Create API > Self-generated.
        Paste the public key above.
STEP 3. Permissions: ONLY "Enable Reading". Leave trading,
        withdrawals, transfers, margin and futures OFF.
        Restrict access to trusted IPs: enter this server's IP.
STEP 4. Binance then shows an API key. Paste it below.
------------------------------------------------------------------
EOF

read -rsp "API key from Binance (input hidden, press Enter when done): " API_KEY
echo
API_KEY="$(printf '%s' "$API_KEY" | tr -d '[:space:]')"
if [[ ! "$API_KEY" =~ ^[A-Za-z0-9]{16,128}$ ]]; then
  echo "That does not look like a Binance API key (letters and digits only). Nothing saved." >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

set_var() {  # set_var NAME VALUE: replace the line or append it
  local name="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v n="$name" -v v="$value" 'BEGIN{done=0} $0 ~ "^"n"=" {print n"="v; done=1; next} {print} END{if(!done) print n"="v}' \
    "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}
set_var BINANCE_API_KEY "$API_KEY"
set_var BINANCE_PRIVATE_KEY_PATH "$KEY_FILE"
unset API_KEY

echo "Saved. $ENV_FILE is readable only by this user and is never uploaded to GitHub."
echo "The key is stored but not used until the read-only Binance connection (module 2) is installed."
