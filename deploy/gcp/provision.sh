#!/usr/bin/env bash
# Create the Google Cloud resources for the WhatsApp email forwarder: a Free Tier e2-micro VM
# (Debian 13, 30 GB standard disk, Shielded VM, no service account) with a reserved static external
# IPv4 address, HTTPS/HTTP open to the world and SSH only through IAP.
#
# Run from your own machine with gcloud installed and authenticated (see docs/setup-gcp.md for
# account, billing and project creation). The project must already exist with billing linked.
#
#   PROJECT_ID=my-project ADMIN_EMAILS=you@example.com bash deploy/gcp/provision.sh
#
# Optional overrides: REGION ZONE VM_NAME MACHINE_TYPE IMAGE_FAMILY IMAGE_PROJECT DISK_GB ADDRESS_NAME TAG
# Safe to re-run: every step checks whether the resource already exists.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your Google Cloud project id}"
REGION="${REGION:-us-central1}"            # Free Tier e2-micro: us-west1, us-central1 or us-east1
ZONE="${ZONE:-${REGION}-a}"
VM_NAME="${VM_NAME:-whatsapp-webhook}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-micro}"
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-13}"  # gcloud defaults to debian-12, so always pass this explicitly
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"
DISK_GB="${DISK_GB:-30}"                   # the Free Tier includes 30 GB-months of standard persistent disk
ADDRESS_NAME="${ADDRESS_NAME:-${VM_NAME}-ip}"
TAG="${TAG:-whatsapp-webhook}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"           # comma-separated Google accounts that may SSH (OS Login + IAP)

log() { printf '\n==> %s\n' "$*"; }

log "Using project $PROJECT_ID (region $REGION, zone $ZONE)"
gcloud config set project "$PROJECT_ID" >/dev/null

log "Enabling the Compute Engine API (no-op if already enabled)"
gcloud services enable compute.googleapis.com

log "Enabling OS Login project-wide (metadata SSH keys are then ignored)"
gcloud compute project-info add-metadata --metadata=enable-oslogin=TRUE

if [[ -n "$ADMIN_EMAILS" ]]; then
  log "Granting SSH (with sudo) and IAP tunnel access to: $ADMIN_EMAILS"
  IFS=',' read -ra EMAILS <<< "$ADMIN_EMAILS"
  for email in "${EMAILS[@]}"; do
    email="$(echo "$email" | xargs)"
    [[ -z "$email" ]] && continue
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="user:$email" \
      --role=roles/compute.osAdminLogin --condition=None >/dev/null
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="user:$email" \
      --role=roles/iap.tunnelResourceAccessor --condition=None >/dev/null
  done
fi

log "Reserving static external IPv4 address $ADDRESS_NAME (Premium tier keeps the free 1 GB egress)"
if ! gcloud compute addresses describe "$ADDRESS_NAME" --region "$REGION" >/dev/null 2>&1; then
  gcloud compute addresses create "$ADDRESS_NAME" --region "$REGION" --network-tier=PREMIUM
fi
IP=$(gcloud compute addresses describe "$ADDRESS_NAME" --region "$REGION" --format='value(address)')

log "Firewall: HTTPS/HTTP from anywhere for tag $TAG; SSH only through IAP (35.235.240.0/20)"
if ! gcloud compute firewall-rules describe "${TAG}-allow-web" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "${TAG}-allow-web" \
    --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 --target-tags="$TAG"
fi
if ! gcloud compute firewall-rules describe "${TAG}-allow-iap-ssh" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "${TAG}-allow-iap-ssh" \
    --direction=INGRESS --action=ALLOW --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 --target-tags="$TAG"
fi
for rule in default-allow-ssh default-allow-rdp; do
  if gcloud compute firewall-rules describe "$rule" --format='value(disabled)' 2>/dev/null | grep -q False; then
    log "Disabling the default open rule $rule"
    gcloud compute firewall-rules update "$rule" --disabled
  fi
done

log "Creating VM $VM_NAME ($MACHINE_TYPE, $IMAGE_FAMILY, ${DISK_GB} GB pd-standard, Shielded VM, no service account)"
if ! gcloud compute instances describe "$VM_NAME" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" \
    --zone "$ZONE" \
    --machine-type "$MACHINE_TYPE" \
    --provisioning-model=STANDARD \
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-standard \
    --network-tier=PREMIUM \
    --address "$IP" \
    --tags "$TAG" \
    --no-service-account --no-scopes \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata enable-oslogin=TRUE \
    --deletion-protection
fi

cat <<EOF

Provisioning complete.

  VM:          $VM_NAME  (zone $ZONE)
  External IP: $IP        (billed at about USD 0.005/hour; the only non-free component)

Next steps:
  1. At Network Solutions create an A record for the webhook hostname -> $IP
     (Type A, Refers to "Other Host", Host Name = the subdomain label, TTL 3600).
  2. Wait until 'dig +short <host>' returns $IP.
  3. Deploy the application:
         PROJECT_ID=$PROJECT_ID ZONE=$ZONE VM_NAME=$VM_NAME DOMAIN=<host> ACME_EMAIL=<admin email> \\
           bash deploy/gcp/deploy.sh
  4. SSH when needed:
         gcloud compute ssh $VM_NAME --zone $ZONE --tunnel-through-iap
  5. Create a budget alert in the console (Billing > Budgets & alerts), e.g. USD 10/month.
EOF
