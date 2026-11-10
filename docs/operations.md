# Operations runbook

The service runs as two systemd units on one VM: `caddy` (TLS + reverse proxy) and
`whatsapp-webhook` (FastAPI webhook + queue worker). Everything below is run on the VM via
`gcloud compute ssh <vm> --zone <zone> --tunnel-through-iap`.

## Daily checks

```bash
systemctl status whatsapp-webhook caddy --no-pager
journalctl -u whatsapp-webhook --since "1 hour ago" --no-pager
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

`/healthz` (localhost only, not exposed through Caddy) reports queue counts, the age of the CRM
index, the last successful send and the last error.

## Operator CLI

`install.sh` places a wrapper at `/usr/local/bin/whatsapp-webhook` that runs the CLI as the service
user with the production environment file (it calls `sudo` itself when needed). The examples below
use `wh` as a short alias for it:

```bash
alias wh=whatsapp-webhook
wh check-config          # effective configuration, secrets redacted
wh test-crm              # log in to VShipCRM, build the customer index, report counts
wh lookup +16505551234   # what the CRM lookup returns for a number
wh test-email            # send a test email through the configured transport
wh meta-check            # WhatsApp token scopes, business numbers, webhook subscription
wh meta-subscribe        # subscribe the app to the Messaging account webhooks
wh stats                 # queue counters and the next pending items
wh list-failed           # dead-letter list
wh replay-failed <key>   # re-queue one dead-lettered message (or: all)
wh purge --days 30       # delete finished rows older than N days (also runs hourly)
```

## Delivery failures

1. A failed send is retried with backoff (15 s, 30 s, 60 s, 120 s, …) for 10 minutes after the
   first attempt (`WORKER_RETRY_WINDOW_SECONDS`).
2. After that the message is marked `failed` (dead letter) and an alert email is queued to
   `ALERT_TO` (defaults to `MAIL_TO`). Alerts use the same mail transport, so if the mail system
   itself is down the alert arrives once it recovers; the dead-letter row is kept regardless.
3. Fix the cause (SMTP credentials, Graph permissions, VShipCRM outage, expired WhatsApp token),
   then `wh replay-failed all`. Replayed messages go through the full pipeline again, including
   media download. WhatsApp keeps inbound media for about 7 days; after that the media download
   fails permanently and the message dead-letters again with that reason.

## Rotating secrets

Edit `/etc/whatsapp-webhook/env`, then `sudo systemctl restart whatsapp-webhook`. The queue and
dead-letter list survive restarts (SQLite in `/var/lib/whatsapp-webhook/queue.db`).

- **WhatsApp System User token**: generate a new one in Meta Business Settings, update
  `WA_ACCESS_TOKEN`, restart, then revoke the old token.
- **Meta App Secret**: resetting it in the App Dashboard invalidates signatures immediately. Update
  `WA_APP_SECRET` and restart within the same minute to avoid rejected webhooks (Meta retries, so
  nothing is lost).
- **VShipCRM password**: update `CRM_USERNAME`/`CRM_PASSWORD`, restart; the service logs in lazily
  and re-logs in on 401.

## Upgrading the application

From your workstation, in the repository:

```bash
PROJECT_ID=<project> DOMAIN=<host> ACME_EMAIL=<email> bash deploy/gcp/deploy.sh
```

`install.sh` is idempotent: it refreshes the code and the virtualenv, keeps the env file and the
database, reloads Caddy and restarts the service.

## Logs and privacy

Application logs go to journald (`journalctl -u whatsapp-webhook`). Phone numbers are masked
(`+16*****1234`) when `LOG_MASK_PHONES=true`. Message bodies are never logged. Raw webhook payloads
are stored in the queue database until the row is purged (`STORE_RETENTION_DAYS`).

## Resource footprint

Expected steady state on the e2-micro: Caddy 30 to 40 MB, the Python service 70 to 100 MB, CPU near
zero. `MemoryMax=600M` in the unit file stops a runaway process from taking the VM down; a 1 GB swap
file is created by `install.sh` as a safety net.
