# Google Cloud: account, project and VM

Written against Google Cloud documentation as of November 2026 (sources at the end). The design
runs one Free Tier **e2-micro** VM. The only unavoidable charge is the external IPv4 address at
about USD 0.005 per hour (roughly USD 3.65 per month); Google bills it even on the free VM.

## 1. Account and billing

1. Choose a durable organisational Google Account to own the project (not a departing employee's
   personal account).
2. Sign up at https://console.cloud.google.com/freetrial if the organisation has never paid for Google
   Cloud, Google Maps Platform or Firebase. You provide a company card (a USD 0 to 1 authorisation
   hold, not a charge) and get a Free Trial billing account with USD 300 of credit for 90 days. The
   country you pick fixes the billing currency and cannot be changed. If you are not eligible, create
   a billing account directly: Console > **Billing > Manage billing accounts > Create account**.
3. **Activate the paid account the same day**: Console > **Welcome** page > toolbar > **Activate** >
   confirm. Without this, Google stops and later deletes everything when the trial ends. You keep the
   unused credit for the rest of the 90 days and the Free Tier is permanent on a paid account.
4. Create a budget alert: **Billing > Budgets & alerts > Create budget**, alerts only, scope this
   project, amount for example USD 10, thresholds 50/90/100 percent, email the billing admins. Alerts
   warn; they do not cap spending.

## 2. Install and sign in to gcloud (administrator's computer)

Debian/Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates gnupg curl
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli
```

macOS: download the archive for your CPU from https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/
(`google-cloud-cli-darwin-arm.tar.gz` or `google-cloud-cli-darwin-x86_64.tar.gz`), extract it and run
`./google-cloud-sdk/install.sh`. Windows: run https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe.

Then sign in:

```bash
gcloud init
```

## 3. Create the project and link billing

Project ids are permanent and globally unique: 6 to 30 characters, lowercase letters, digits and
hyphens, starting with a letter, not ending with a hyphen, never reused.

```bash
gcloud projects create <project-id> --name="WhatsApp Webhook" --set-as-default
gcloud billing accounts list
gcloud billing projects link <project-id> --billing-account=0X0X0X-0X0X0X-0X0X0X
gcloud config set compute/region us-east1
gcloud config set compute/zone us-east1-b
```

Any of `us-west1`, `us-east1`, `us-east1` is Free Tier eligible. Pick one and keep it.

## 4. Provision the VM

From the repository on your computer:

```bash
PROJECT_ID=<project-id> ADMIN_EMAILS=you@example.com bash deploy/gcp/provision.sh
```

The script is idempotent and does, in order: enable the Compute Engine API; turn on OS Login
project-wide; grant the listed Google accounts SSH-with-sudo and IAP tunnel roles; reserve a
regional static external IPv4 address (Premium tier, which keeps the free 1 GB of egress); create
firewall rules allowing 80/443 from anywhere and SSH only from Google's IAP range
`35.235.240.0/20`, and disable the default open SSH/RDP rules; create the VM as `e2-micro`, image
family `debian-13` (Python 3.13, supported to June 2030), 30 GB `pd-standard` boot disk (the Free
Tier allowance), Shielded VM with Secure Boot, **no service account** (the VM talks only to Meta,
Microsoft and VShipCRM), OS Login, deletion protection. It prints the IP and the next steps.

Why these choices: the default Compute Engine service account can carry the Editor role on a
stand-alone project, so a compromised webhook process must not inherit it; OS Login plus IAP means
no SSH port is open to the internet; `gcloud` defaults to `debian-12`, so the script passes
`debian-13` explicitly.

## 5. DNS, deploy, secrets

1. At Network Solutions create the A record for the webhook hostname pointing at the printed IP
   (docs/setup-microsoft365-email.md, section 3b). Wait until `dig +short <host>` returns it. Caddy
   is only started once the name resolves to the VM, to avoid burning Let's Encrypt rate limits.
2. Deploy:

   ```bash
   PROJECT_ID=<project-id> DOMAIN=<host> ACME_EMAIL=<admin email> bash deploy/gcp/deploy.sh
   ```

   This copies the tracked files to the VM over IAP and runs `deploy/install.sh`, which installs
   Caddy from its official repository, creates the service user, the virtualenv, the env file, the
   systemd unit, a 1 GB swap file and journald limits.
3. Fill in the secrets and start:

   ```bash
   gcloud compute ssh whatsapp-webhook --zone us-east1-b --tunnel-through-iap
   sudo nano /etc/whatsapp-webhook/env        # replace every CHANGE_ME
   sudo systemctl restart whatsapp-webhook && journalctl -u whatsapp-webhook -f
   ```

4. Smoke tests on the VM (see docs/operations.md for the `wh` alias): `wh check-config`,
   `wh test-crm`, `wh test-email`, `wh meta-check`, then the Meta webhook verification
   (docs/setup-meta-whatsapp.md, section 4).

## 6. Costs to expect

| Item | Free Tier | Expected charge |
|---|---|---|
| e2-micro, 24x7 | 1 instance-month in us-west1/us-east1/us-east1 | 0 |
| 30 GB standard persistent disk | 30 GB-months | 0 |
| Egress from North America | 1 GB/month (Premium tier) | 0 at this traffic level |
| External IPv4 in use | 1 hour/month only | about USD 3.65/month |

Check **Billing > Reports** after a few days. If the VM is ever deleted, also release the address
(`gcloud compute addresses delete whatsapp-webhook-ip --region us-east1`); an unattached
reserved address costs more than an attached one.

## 7. Sources

- [Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features),
  [Free Trial terms](https://cloud.google.com/terms/free-trial),
  [Create a billing account](https://docs.cloud.google.com/billing/docs/how-to/create-billing-account),
  [Install gcloud](https://docs.cloud.google.com/sdk/docs/install),
  [Creating and managing projects](https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects),
  [gcloud billing projects link](https://docs.cloud.google.com/sdk/gcloud/reference/billing/projects/link),
  [Network pricing (external IP)](https://cloud.google.com/vpc/network-pricing),
  [OS images (Debian 13)](https://docs.cloud.google.com/compute/docs/images/os-details),
  [OS Login](https://docs.cloud.google.com/compute/docs/oslogin),
  [IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding),
  [Shielded VM](https://docs.cloud.google.com/compute/shielded-vm/docs/modifying-shielded-vm),
  [Service accounts on VMs](https://docs.cloud.google.com/compute/docs/access/service-accounts),
  [Reserve a static external IP](https://docs.cloud.google.com/compute/docs/ip-addresses/reserve-static-external-ip-address),
  [Blocked outbound port 25](https://docs.cloud.google.com/vpc/docs/firewalls#blockedtraffic).
