#!/usr/bin/env bash
# Idempotent on-VM installer for the WhatsApp email forwarder (Debian 12/13).
#
# Usage (as root on the VM):
#   DOMAIN=wa.example.com ACME_EMAIL=admin@example.com bash install.sh /path/to/source
#
# What it does:
#   * installs Python, Caddy (official apt repository) and unattended-upgrades
#   * creates the unprivileged service user, /opt/whatsapp-webhook (code + venv),
#     /etc/whatsapp-webhook/env (secrets, mode 640) and /var/lib/whatsapp-webhook (queue + media spool)
#   * installs the systemd unit and the Caddyfile, adds a 1 GB swap file and caps journald
#   * starts the service only when the env file has been filled in
#
# Re-running it upgrades the code and dependencies without touching the env file or the queue.
set -euo pipefail

SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_DIR=/opt/whatsapp-webhook
ENV_DIR=/etc/whatsapp-webhook
ENV_FILE="$ENV_DIR/env"
DATA_DIR=/var/lib/whatsapp-webhook
SERVICE_USER=whwebhook
DOMAIN="${DOMAIN:-}"
ACME_EMAIL="${ACME_EMAIL:-}"

log() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi
if [[ -z "$DOMAIN" ]]; then
  echo "DOMAIN is required, e.g. DOMAIN=whatsapphook.vship2000.com" >&2
  exit 1
fi
if [[ -z "$ACME_EMAIL" ]]; then
  # a real mailbox: it registers the ACME account and enables Caddy's ZeroSSL fallback CA
  echo "ACME_EMAIL is required, e.g. ACME_EMAIL=admin@vship2000.com" >&2
  exit 1
fi
if [[ ! -f "$SRC/requirements.txt" || ! -d "$SRC/app" ]]; then
  echo "source directory $SRC does not look like the repository" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive

log "Installing base packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-venv python3-pip ca-certificates curl gnupg rsync \
  debian-keyring debian-archive-keyring apt-transport-https unattended-upgrades

log "Installing Caddy from the official repository"
CADDY_KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
CADDY_LIST=/etc/apt/sources.list.d/caddy-stable.list
# Download to temporary files, check them, then move them into place, so an interrupted download
# never leaves an empty key or list behind that a re-run would trust.
if [[ ! -s "$CADDY_KEYRING" ]] || ! gpg --show-keys "$CADDY_KEYRING" >/dev/null 2>&1; then
  tmp_key=$(mktemp)
  curl -1sSLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor > "$tmp_key"
  gpg --show-keys "$tmp_key" >/dev/null
  install -m 644 "$tmp_key" "$CADDY_KEYRING"
  rm -f "$tmp_key"
fi
if [[ ! -s "$CADDY_LIST" ]] || ! grep -q 'dl.cloudsmith.io' "$CADDY_LIST"; then
  tmp_list=$(mktemp)
  curl -1sSLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > "$tmp_list"
  grep -q 'dl.cloudsmith.io' "$tmp_list"
  install -m 644 "$tmp_list" "$CADDY_LIST"
  rm -f "$tmp_list"
fi
apt-get update -qq
apt-get install -y -qq caddy
# Debian's own archive ships a years-old Caddy; make sure the package came from the Caddy repository.
# (The output is captured first: piping apt-cache into grep -q fails under pipefail via SIGPIPE.)
caddy_policy=$(apt-cache policy caddy)
if [[ "$caddy_policy" != *dl.cloudsmith.io* ]]; then
  echo "caddy was not installed from dl.cloudsmith.io; check $CADDY_LIST" >&2
  exit 1
fi
echo "caddy $(caddy version)"

log "Creating service user and directories"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$APP_DIR" "$ENV_DIR" "$DATA_DIR/spool"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

log "Copying application code to $APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '*.db*' \
  "$SRC/" "$APP_DIR/"
chown -R root:root "$APP_DIR"

log "Installing Python dependencies into a virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

log "Preparing the environment file"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  {
    echo ""
    echo "# --- values set by install.sh ---"
    echo "STORE_PATH=$DATA_DIR/queue.db"
    echo "SPOOL_DIR=$DATA_DIR/spool"
    echo "APP_BIND_HOST=127.0.0.1"
    echo "APP_BIND_PORT=8000"
  } >> "$ENV_FILE"
  echo "created $ENV_FILE from .env.example - fill in the CHANGE_ME values"
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "Installing the operator wrapper /usr/local/bin/whatsapp-webhook"
cat > /usr/local/bin/whatsapp-webhook <<EOF
#!/usr/bin/env bash
# Runs the operator CLI as the service user with the production environment file.
set -euo pipefail
if [[ \$EUID -ne 0 ]]; then
  exec sudo "\$0" "\$@"
fi
cd "$APP_DIR"
exec runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m app.cli --env-file "$ENV_FILE" "\$@"
EOF
chmod 755 /usr/local/bin/whatsapp-webhook

log "Installing systemd unit"
install -m 644 "$APP_DIR/deploy/systemd/whatsapp-webhook.service" /etc/systemd/system/whatsapp-webhook.service
systemctl daemon-reload
systemctl enable whatsapp-webhook >/dev/null

log "Installing Caddyfile for $DOMAIN"
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__ACME_EMAIL__/$ACME_EMAIL/g" \
  "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl enable caddy >/dev/null

# Only start Caddy once DNS points here: repeated failed ACME challenges against a wrong record
# count toward Let's Encrypt's rate limits and can block certificate issuance for hours or days.
VM_IP=$(curl -fs -H 'Metadata-Flavor: Google' \
  'http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip' \
  2>/dev/null || true)
RESOLVED=$(getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | tr '\n' ' ' || true)
if [[ -n "$VM_IP" && "$RESOLVED" != *"$VM_IP"* && "${FORCE_CADDY:-0}" != "1" ]]; then
  echo "WARNING: $DOMAIN resolves to [${RESOLVED:-nothing}] but this VM's external IP is $VM_IP."
  echo "         Caddy was configured but NOT started. Fix the DNS A record, then run:"
  echo "         sudo systemctl restart caddy   (or re-run install.sh)"
  systemctl stop caddy >/dev/null 2>&1 || true
else
  systemctl restart caddy
fi

log "Swap file (1 GB) as a safety net on the 1 GB VM"
if ! swapon --show | grep -q '/swapfile'; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Capping journald disk usage and enabling unattended security upgrades"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/size.conf <<'EOF'
[Journal]
SystemMaxUse=200M
EOF
systemctl restart systemd-journald
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
# Debian's default origin patterns only cover Debian's own archives; include Caddy's repository so
# Caddy receives updates too (the package restarts the service on upgrade).
cat > /etc/apt/apt.conf.d/51unattended-upgrades-caddy <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=cloudsmith/caddy/stable,codename=any-version";
};
EOF

log "Checking configuration"
# only real assignments count, e.g. MAIL_TO=CHANGE_ME or MAIL_TO="CHANGE_ME"; comments are ignored
placeholders=$(grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=["'"'"']?CHANGE_ME' "$ENV_FILE" | cut -d= -f1 | tr '\n' ' ' || true)
if [[ -n "$placeholders" ]]; then
  echo "These settings in $ENV_FILE still hold CHANGE_ME: $placeholders"
  echo "Edit it, then run:  sudo systemctl restart whatsapp-webhook && journalctl -u whatsapp-webhook -f"
else
  if /usr/local/bin/whatsapp-webhook check-config >/dev/null; then
    systemctl restart whatsapp-webhook
    sleep 2
    systemctl --no-pager --lines=5 status whatsapp-webhook || true
  else
    echo "check-config failed; fix $ENV_FILE and run: systemctl restart whatsapp-webhook" >&2
  fi
fi

cat <<EOF

Done. Useful commands:
  journalctl -u whatsapp-webhook -f        # application logs
  journalctl -u caddy -f                   # TLS / proxy logs
  curl -s "https://$DOMAIN/webhook?hub.mode=subscribe&hub.verify_token=<WA_VERIFY_TOKEN>&hub.challenge=123"
  whatsapp-webhook check-config            # operator CLI (runs as $SERVICE_USER with $ENV_FILE)
  whatsapp-webhook test-crm
  whatsapp-webhook test-email
  whatsapp-webhook meta-check
EOF
