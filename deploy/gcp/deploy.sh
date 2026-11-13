#!/usr/bin/env bash
# Copy this repository to the VM and run deploy/install.sh there. Re-run to upgrade.
#
#   PROJECT_ID=my-project DOMAIN=whatsapphook.vship2000.com ACME_EMAIL=admin@vship2000.com bash deploy/gcp/deploy.sh
#
# Optional: ZONE (default us-east1-b), VM_NAME (default whatsapp-webhook)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
DOMAIN="${DOMAIN:?set DOMAIN to the webhook hostname, e.g. whatsapphook.vship2000.com}"
ACME_EMAIL="${ACME_EMAIL:?set ACME_EMAIL to a real mailbox for certificate notices, e.g. admin@vship2000.com}"
ZONE="${ZONE:-us-east1-b}"
VM_NAME="${VM_NAME:-whatsapp-webhook}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCHIVE="$(mktemp -t whatsapp-webhook-XXXXXX.tgz)"
trap 'rm -f "$ARCHIVE"' EXIT
# Unique name on the VM: a leftover /tmp file owned by another login (sticky /tmp) cannot be overwritten.
REMOTE_ARCHIVE="/tmp/whatsapp-webhook-$(date +%Y%m%d%H%M%S)-$$.tgz"

echo "==> Packaging tracked files from $REPO_ROOT"
(cd "$REPO_ROOT" && git ls-files -z | tar --null -czf "$ARCHIVE" -T -)

echo "==> Uploading to $VM_NAME ($ZONE) via IAP"
gcloud compute scp --project "$PROJECT_ID" --zone "$ZONE" --tunnel-through-iap \
  "$ARCHIVE" "$VM_NAME:$REMOTE_ARCHIVE"

echo "==> Running install.sh on the VM"
gcloud compute ssh "$VM_NAME" --project "$PROJECT_ID" --zone "$ZONE" --tunnel-through-iap --command \
  "sudo rm -rf /tmp/whatsapp-webhook-src && sudo mkdir -p /tmp/whatsapp-webhook-src \
   && sudo tar -xzf '$REMOTE_ARCHIVE' -C /tmp/whatsapp-webhook-src && rm -f '$REMOTE_ARCHIVE' \
   && sudo env DOMAIN='$DOMAIN' ACME_EMAIL='$ACME_EMAIL' bash /tmp/whatsapp-webhook-src/deploy/install.sh /tmp/whatsapp-webhook-src"

cat <<EOF

Deployed. If this was the first run, fill in the secrets:
  gcloud compute ssh $VM_NAME --zone $ZONE --tunnel-through-iap
  sudo nano /etc/whatsapp-webhook/env
  sudo systemctl restart whatsapp-webhook && journalctl -u whatsapp-webhook -f
EOF
