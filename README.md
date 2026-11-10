# WhatsApp → Email forwarder with VShipCRM Reply-To lookup

A small Python service that receives every inbound WhatsApp Business (Cloud API) message through
Meta's webhook and forwards it by email. The sender's phone number is looked up in VShipCRM; when a
customer matches, all of that customer's email addresses become the email's **Reply-To**. When the
lookup fails (no match, no phone number in the payload, or the CRM is unreachable) the email carries
the sender's WhatsApp display name and says why no Reply-To was set. Media is attached; files too
large for one email are split across numbered emails with rebuild instructions.

It runs on a single Google Compute Engine **e2-micro** (Free Tier) behind **Caddy** (automatic
HTTPS). See [PLAN.md](PLAN.md) for the design and decisions.

```
Meta WhatsApp Cloud API ──HTTPS──► Caddy ──► FastAPI /webhook ──► SQLite queue ──► worker
                                                                                   ├─ VShipCRM lookup (in-memory customer index)
                                                                                   ├─ media download (Graph API)
                                                                                   └─ email (SMTP or Microsoft Graph)
```

## Repository layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: `GET /webhook` (verification), `POST /webhook` (signed payloads), `GET /healthz` |
| `app/worker.py` | Queue worker: lookup → media → compose → send, retries for 10 min, dead-letter + alert |
| `app/whatsapp/` | Payload parser (all message types) and media downloader |
| `app/crm/` | VShipCRM client, phone normalisation, customer phone index |
| `app/mail/` | Email composer (Reply-To, threading headers, splitting) and SMTP / Graph transports |
| `app/store.py` | SQLite queue with de-duplication on the WhatsApp message id |
| `app/cli.py` | Operator commands (`test-crm`, `lookup`, `test-email`, `list-failed`, `replay-failed`, …) |
| `deploy/` | Caddyfile, systemd unit, on-VM installer, GCP provisioning and deploy scripts |
| `docs/` | Setup guides (Microsoft 365 mail + DNS, Meta/WhatsApp, Google Cloud) and the operations runbook |
| `tests/` | pytest suite incl. an end-to-end run through a local SMTP sink |

## Setup guides

1. [Google Cloud project and VM](docs/setup-gcp.md)
2. [Microsoft 365 sending path, SPF, DKIM, DMARC and Network Solutions DNS](docs/setup-microsoft365-email.md)
3. [Meta app, System User token and webhook subscription](docs/setup-meta-whatsapp.md)
4. [Operations runbook](docs/operations.md)

## Quick start (deployment)

```bash
# 1. Create the VM, static IP and firewall rules (run on your machine, gcloud logged in as the project owner)
PROJECT_ID=<project> ADMIN_EMAILS=thevshipapp@gmail.com bash deploy/gcp/provision.sh

# 2. Create the DNS A record at Network Solutions, wait for it to resolve, then deploy
PROJECT_ID=<project> DOMAIN=whatsapphook.vship2000.com ACME_EMAIL=thevshipapp@gmail.com bash deploy/gcp/deploy.sh

# 3. Fill in the secrets on the VM and start the service
gcloud compute ssh whatsapp-webhook --zone us-east1-b --tunnel-through-iap
sudo nano /etc/whatsapp-webhook/env      # replace every CHANGE_ME
sudo systemctl restart whatsapp-webhook && journalctl -u whatsapp-webhook -f

# 4. Point Meta at https://whatsapphook.vship2000.com/webhook with your verify token and subscribe to "messages"
```

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in values; for dev see the local sink settings below
ruff check . && ruff format --check . && pytest -q
python -m app.cli run           # http://127.0.0.1:8000
```

For development without Microsoft 365 or the CRM, run a local SMTP sink with
`python -m aiosmtpd -n -l 127.0.0.1:2525` and set in `.env`:
`MAIL_TRANSPORT=smtp SMTP_HOST=127.0.0.1 SMTP_PORT=2525 SMTP_SECURITY=none SMTP_AUTH=none
CRM_ENABLED=false`, and give the `OAUTH_*`, `CRM_*` and `WA_*` placeholders any non-placeholder
values (secrets need at least 16 characters).

## Behaviour summary

- Responds `200` to Meta immediately; all work happens in the background queue. Duplicate deliveries
  (Meta retries) are ignored by message id. Delivery receipts (`statuses`) are not forwarded.
- Every inbound type is forwarded: text, image, video, audio/voice, document, sticker, location,
  contact cards, interactive replies, buttons, reactions, orders, system notices and unsupported types.
- Phone numbers can be absent (WhatsApp usernames / business-scoped user ids). The parser treats the
  phone as optional; without one the CRM lookup is skipped and the display name is used.
- CRM lookup: the Customer list is downloaded and indexed in memory (E.164 and last-10-digit keys),
  refreshed every 15 minutes and on a cache miss. Match ranking: exact number > suffix, mobile >
  business phone, active > inactive, most recently updated.
- Emails: fixed `From` mailbox with the sender's name in the display part, `Reply-To` = all customer
  emails (configurable), `X-WhatsApp-*` headers for mail rules, text + HTML bodies, attachments,
  numbered parts when oversized. Deterministic `Message-ID`s and threading headers for quoted replies
  apply on SMTP and to Graph emails under about 3.5 MB encoded; larger Graph emails are sent as drafts
  and carry Exchange's own `Message-ID` and at most five custom headers.
- Failures retry for 10 minutes (longer if Microsoft asks to wait with `Retry-After`), then
  dead-letter with an alert email; alerts retry for 12 hours; `replay-failed` re-queues.
