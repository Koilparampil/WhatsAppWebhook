# Plan: WhatsApp Business → Email forwarder with VShipCRM Reply-To lookup

Status: **Implemented**; awaiting the owner's live rollout (plan confirmed 2026-11-09).
Last updated: 2026-11-10. Sections 1 to 9 describe the system as built; the decisions log records why.

## 0. Decisions log

| Topic | Decision | Effect |
|---|---|---|
| Repository | github.com/Koilparampil/WhatsAppWebhook (branch `main`) | all code, deploy assets and docs live here |
| Email system | Microsoft 365 (Exchange Online); recipients are inside the same tenant; DNS at Network Solutions | sending path researched: **Microsoft Graph sendMail** recommended, OAuth SMTP AUTH kept as fallback, Direct Send ruled out (Google Cloud blocks outbound port 25). See docs/setup-microsoft365-email.md |
| CRM scope | existing VShipCRM service account; search the **Customer** list only; phone numbers are mostly US | `CRM_ENTITIES=Customer`, `CRM_DEFAULT_REGION=US` |
| Reply-To | when a customer matches, **all** of its email addresses go into Reply-To | `CRM_REPLY_TO_ALL_EMAILS=true` |
| Meta | app, WhatsApp Business Account and business number exist; System User token guide needed | docs/setup-meta-whatsapp.md, `meta-check` / `meta-subscribe` CLI |
| Media | attach everything with **no size cap**; media that exceeds the email size limit is **split across several emails** | numbered parts `name.001`, `name.002`… with rebuild instructions; `MAIL_MAX_MESSAGE_BYTES` defaults to 32 MB (Exchange Online default send limit is 35 MB) |
| GCP | project does not exist yet; user runs the provisioning scripts | docs/setup-gcp.md, deploy/gcp/provision.sh, deploy/gcp/deploy.sh |
| Retries | failed sends retry for **10 minutes**, then dead-letter and an **alert email** | `WORKER_RETRY_WINDOW_SECONDS=600`, `ALERT_ON_DEAD_LETTER=true` |
| Graph API version | v27.0 (released 4 Nov 2026) | `WA_GRAPH_API_VERSION=v27.0` |
| Media download failure | treated as a retriable failure of the whole message (no email without its attachment); dead-letter + alert after the window | protects the "attach all media" requirement; `replay-failed` re-sends |
| Mail transport (confirmed) | **Microsoft Graph** from an unlicensed **shared mailbox** (proposed `whatsapp@vship2000.com`); Global Admin available to run the Exchange Online steps | `MAIL_TRANSPORT=graph`; docs/setup-microsoft365-email.md section 2 |
| Message size | keep the 32 MB split threshold; worked example for raising tenant limits documented | `MAIL_MAX_MESSAGE_BYTES=32000000` |
| Timezone | New York | `MAIL_TIMEZONE=America/New_York` |
| Hostname / DNS | `whatsapphook.vship2000.com`; nameservers `ns89/ns90.worldnic.com` (Network Solutions); SPF, DKIM CNAMEs and DMARC `p=none` already published, only the A record is missing | docs/setup-microsoft365-email.md section 3 |
| Google Cloud | owner account `thevshipapp@gmail.com`; the external IP charge is accepted; proposed region `us-east1` (closest Free Tier region to New York) | deploy/gcp/provision.sh defaults |
| CRM records | inactive customers are not searched | `CRM_INCLUDE_INACTIVE=false` |
| Alerts | same recipients as the forwarded mail; alerts retry for **12 hours**; no alerts about alerts | `ALERT_RETRY_WINDOW_SECONDS=43200` |
| Split format (confirmed) | `name.001`, `name.002`, …; every email states its part number and the `copy /b`, `cat` and 7-Zip rebuild commands | app/mail/compose.py |

---

## 1. Goal

A small Python service on one Google Compute Engine VM behind Caddy (automatic HTTPS for
`whatsapphook.vship2000.com`) that:

1. Receives every inbound WhatsApp Business (Cloud API) message through Meta's webhook.
2. Looks the sender's phone number up in VShipCRM (active Customer records).
   - Match found: **all** of the customer's email addresses become the email's **Reply-To**.
   - No match, no phone number in the payload, a customer without email, or a CRM error: the email
     carries the sender's **WhatsApp display name** (and username/BSUID when present) and states why
     no Reply-To was set.
3. Sends the message (text, captions, location, contact cards, interactive replies, reactions, orders,
   media as attachments, split into numbered parts when too large) to the Microsoft 365 tenant through
   **Microsoft Graph**, with SMTP (OAuth) as a fallback transport.

## 2. Research findings that shape the design

Primary sources are cited in the setup guides under `docs/`.

### 2.1 WhatsApp Cloud API webhooks (Meta)

| Fact | Consequence |
|---|---|
| Verification is a `GET` with `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`. | `GET /webhook` echoes the challenge when the token matches (constant-time compare). |
| Every `POST` carries `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body keyed with the App Secret>`. | Verified on the raw bytes before parsing; 403 otherwise. |
| Meta retries failed deliveries (36 hours on the generic Graph page, up to 7 days on the WhatsApp pages) and duplicates happen. | 200 at once after queueing; de-duplication on the message id (wamid). |
| Payloads up to 3 MB, batched. | Every `entry[].changes[]` is processed; Caddy rejects bodies over 4 MB. |
| Delivery receipts (`statuses[]`) arrive on the same `messages` field. | Counted and ignored, never emailed. |
| Business-scoped user ids (`messages[].from_user_id`, `contacts[].user_id`) since April 2026; the phone number (`from`, `wa_id`) may be omitted for username users. | Phone is optional; without it the lookup is skipped and the display name, username and BSUID are used. |
| Media is a numeric id: `GET /{version}/{media-id}` gives a URL valid 5 minutes, fetched with the same bearer token; ids live 7 days; documents up to 100 MB. | Downloaded at processing time, streamed to a spool file; ids are validated as numeric before use. |
| Graph API v27.0 is current (4 Nov 2026). | `WA_GRAPH_API_VERSION=v27.0`. |
| Webhook routing precedence: phone-number override, then WABA override, then the app callback. | `whatsapp-webhook meta-check` reports any override. |

Inbound types handled: `text`, `image`, `audio` (voice notes included), `video`, `document`, `sticker`,
`location`, `contacts`, `interactive`, `button`, `reaction`, `order`, `request_welcome`, `system`,
`unsupported` (with Meta's error text), unknown future types (raw message included), plus `context`
(quoted replies, forwarded) and `referral` (click-to-WhatsApp ads).

### 2.2 VShipCRM API (`https://vship2000-prod-api.azurewebsites.net`)

- `POST /api/Auth/login {username, password}` returns a JWT whose location in the response body is
  undocumented; the client finds any JWT-shaped value (known keys first) and reports where it was
  found (`whatsapp-webhook test-crm`). Tokens are refreshed 60 s before `exp` and after any 401.
- There is **no phone search**. `GET /api/Customer` returns the full list in the envelope
  `{isSuccess, isFailure, error, value: [...]}` with `mobile`, `businessPhone`, `email1..email4`,
  `companyName`, `contactPerson`, `active`. Other party lists exist (Shipper, Consignee, Agent,
  Exporter, NotifyingParty, Trucker, User) but only Customer is searched (decision log).

### 2.3 Google Cloud

- Free Tier: one e2-micro (0.25 vCPU sustained, 1 GB RAM) in us-west1, us-central1 or us-east1,
  30 GB-months standard disk, 1 GB/month Premium-tier egress. The external IPv4 is billed at about
  USD 0.005 per hour (about USD 3.65 per month), accepted by the owner.
- Outbound TCP 25 is blocked by default, which rules out Microsoft 365 Direct Send.
- Measured footprint: Caddy 30 to 40 MB; the service 70 to 100 MB idle and about 170 MB at peak while
  splitting a 100 MB document through Graph. Two systemd services, no containers.

### 2.4 Microsoft 365

- Graph `sendMail` takes a base64 MIME message; anything larger than about 3.5 MB encoded goes through
  a draft (inline attachments under 3 MB, upload sessions of 3 to 150 MB with sequential PUTs), which
  needs `Mail.ReadWrite` as well as `Mail.Send` (RBAC role "Application Mail Full Access" scoped to the
  relay mailbox). Graph accepts at most five custom headers on a draft.
- Exchange Online defaults: 35 MB send, 36 MB receive, measured after base64; 30 messages per minute
  per mailbox; HTTP 429 with `Retry-After` when throttled.
- Basic-auth SMTP is disabled by default from the end of December 2026; the SMTP fallback uses OAuth.

## 3. Architecture

```
 WhatsApp user ──► Meta WhatsApp Cloud API
                          │  HTTPS POST /webhook  (X-Hub-Signature-256)
                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ GCE e2-micro, Debian 13, us-east1-b, static IPv4, tcp/80,443 open   │
   │                                                                     │
   │  Caddy :443  automatic TLS for whatsapphook.vship2000.com           │
   │     │ only /webhook is proxied (4 MB limit); everything else → 404  │
   │     ▼                                                               │
   │  uvicorn + FastAPI on 127.0.0.1:8000 (systemd, unprivileged)        │
   │     ├─ GET  /webhook  verification handshake                       │
   │     ├─ POST /webhook  verify HMAC → parse → INSERT OR IGNORE        │
   │     │                 into the SQLite queue → 200                   │
   │     ├─ GET  /healthz  localhost only (503 when the worker is dead)  │
   │     └─ background tasks in the same process                         │
   │          • worker: CRM lookup → media download → split plan →      │
   │            build one part at a time → send → done / retry / dead    │
   │          • CRM refresher (every 15 min), maintenance purge (hourly) │
   └─────────────────────────────────────────────────────────────────────┘
        │ VShipCRM API          │ graph.facebook.com     │ graph.microsoft.com
```

A persistent SQLite queue gives durability across restarts, de-duplication, retries and a dead-letter
list with no extra services. One process means one CRM index, one worker and one database writer.

## 4. Repository layout

```
WhatsAppWebhook/
├── README.md, PLAN.md, .env.example, pyproject.toml, requirements*.txt, .gitattributes
├── app/
│   ├── main.py           FastAPI app, routes, lifespan (worker, CRM warm-up/refresher, maintenance)
│   ├── config.py         Settings (environment / env file), validation incl. placeholder secrets
│   ├── security.py       signature and verify-token checks
│   ├── store.py          SQLite queue: dedup, claim, retry, crash count, part progress, dead letter
│   ├── worker.py         processing loop, retries, Retry-After pause, crash recovery, alerts
│   ├── cli.py            operator commands (installed as /usr/local/bin/whatsapp-webhook)
│   ├── whatsapp/         parser.py, media.py (download), admin.py (meta-check / meta-subscribe)
│   ├── crm/              client.py (login, lists), phones.py (E.164, ranking), directory.py (index)
│   └── mail/             compose.py (emails, split plan, rebuild commands), transports.py (Graph, SMTP)
├── deploy/               Caddyfile, systemd unit, install.sh, gcp/provision.sh, gcp/deploy.sh
├── docs/                 setup-gcp.md, setup-microsoft365-email.md, setup-meta-whatsapp.md, operations.md
├── tests/                121 pytest tests incl. end-to-end through a local SMTP sink
└── .github/              CI (ruff + pytest), Dependabot
```

Stack: Python 3.13 (Debian 13), FastAPI with one uvicorn worker, httpx, pydantic-settings,
`phonenumbers`, stdlib `sqlite3`, `smtplib`, `email`.

## 5. Behaviour

### 5.1 HTTP endpoints

- `GET /webhook`: `hub.mode=subscribe` and a matching `hub.verify_token` → 200 with `hub.challenge`; else 403.
- `POST /webhook`: signature check on the raw body (403 on failure), JSON parse (400 on failure), each
  `messages[]` entry stored with `INSERT OR IGNORE` keyed by wamid, response
  `{"status": "ok", "queued": N, "duplicates": M}`. Statuses and other fields are logged and dropped.
  With `WA_PHONE_NUMBER_ID` set, messages for other business numbers are skipped and logged.
- `GET /healthz`: queue counters, worker state, CRM index age; reachable on localhost only.

### 5.2 Worker

1. Parse the stored envelope into an `InboundMessage`.
2. CRM lookup (never fatal: an exception becomes a lookup error in the email).
3. Download the media (if any) to the spool. A download failure fails the attempt: no email is sent
   without its attachment.
4. Plan the email(s): one email when the encoded attachment fits `MAIL_MAX_MESSAGE_BYTES`
   (32,000,000), otherwise parts of a fixed size cut from the file. The plan depends only on the file
   size and the limit, and its signature is stored, so a retry after a partial send resumes at the
   next part, or re-sends every part if the cut changed.
5. Build and send one part at a time (encoding in a thread; peak memory about 140 MB for a 100 MB file).
6. Outcome: done; or retry with backoff 15, 30, 60, 120, 120 … seconds for 10 minutes after the first
   attempt (12 hours for alert emails), waiting at least as long as a `Retry-After` header asks and
   pausing all sends meanwhile; or dead-letter, which queues an alert email for message items (alerts
   never raise alerts). Writes that hit a busy database are retried; the loop itself never dies.
7. Startup: rows left in `processing` were interrupted by a crash; they are retried with backoff and
   dead-lettered after 3 crashes. A graceful shutdown hands the in-flight row back without counting a
   crash. Partial downloads in the marked spool directory are deleted.
8. Finished rows (and their raw payloads) are purged after `STORE_RETENTION_DAYS` (30).

### 5.3 CRM lookup

- Index of active Customer records by E.164 number and by last-10-digit suffix (for numbers
  libphonenumber rejects). Fields holding several numbers ("650-555-1234 / Fax 650-555-9999") are
  split. US is the default region.
- Refreshed every 15 minutes; on a miss, refreshed again if the index is over 60 s old. Refresh
  attempts are limited to one per 60 s even while the CRM is failing, and concurrent callers share one
  attempt. A stale index keeps serving during an outage.
- Ranking for shared numbers: exact over suffix, mobile over business phone, active over inactive,
  most recently updated.
- Outcomes (also the `X-WhatsApp-CRM-Match` header): `customer:<id>`, `no_match`, `no_email`,
  `no_phone`, `error`, `disabled`. A miss while the CRM cannot be reached is reported as `error`.

### 5.4 Email

- `From`: `"<sender> via WhatsApp" <MAIL_FROM>`; the display part is stripped of anything address-like
  and of control and bidi characters. `To`: `MAIL_TO`. `Reply-To`: all customer emails when matched,
  else omitted (or `MAIL_FALLBACK_REPLY_TO`).
- `Subject`: `[WhatsApp] <sender> (<phone>): <text preview or "sent a document: x.pdf">`, plus
  `[part i/N]` for split media.
- Body (plain text and HTML): sender, phone, username, BSUID, time in New York, business number,
  type; the message content; the CRM result and why there is no Reply-To; the attachment, with
  `copy /b`, `cat` and 7-Zip rebuild instructions for split media (names quoted safely; wildcards
  beyond 12 parts); message id, quoted message, forwarding and ad referral details. Only the service's
  own map links are clickable.
- Headers: deterministic `Message-ID`, `In-Reply-To`/`References` for quoted replies and for later
  parts, `X-WhatsApp-Message-Id`, `-From`, `-BSUID`, `-Type`, `-CRM-Match`, `-Part`. On the Graph
  draft path (emails over about 3.5 MB encoded) Exchange assigns its own `Message-ID` and only five
  custom headers are sent.
- One-way only: replying to the email reaches the CRM contact's mailbox, not WhatsApp.

### 5.5 Configuration

Every setting is documented in `.env.example`; on the VM the values live in
`/etc/whatsapp-webhook/env` (root:whwebhook, mode 640). Placeholder or short secrets are rejected at
startup, and the CLI exits with code 78 on a configuration error so systemd does not restart-loop.

## 6. Deployment

### 6.1 `deploy/gcp/provision.sh` (run by the owner with gcloud)

Enables Compute Engine; turns on OS Login; grants `ADMIN_EMAILS` the OS Admin Login and IAP tunnel
roles; removes an automatic Editor grant from the default compute service account; reserves a Premium
static IPv4 in us-east1; opens tcp/80,443 to tag `whatsapp-webhook` and tcp/22 only from IAP
(35.235.240.0/20), disabling the default open SSH/RDP rules; creates `whatsapp-webhook` as e2-micro,
us-east1-b, debian-13, 30 GB pd-standard, Shielded VM, no service account, deletion protection.

### 6.2 `deploy/gcp/deploy.sh` and `deploy/install.sh`

`deploy.sh` packs the tracked files, copies them over IAP and runs `install.sh` as root, which is
idempotent: base packages; Caddy from its official repository (verified origin); service user
`whwebhook`; code in `/opt/whatsapp-webhook` with a virtualenv; env file created once from
`.env.example`; operator wrapper `/usr/local/bin/whatsapp-webhook`; systemd unit; Caddyfile (Caddy is
started only once DNS points at the VM); 1 GB swap; journald cap; unattended upgrades including Caddy.
The service is restarted only when no placeholder values remain and `check-config` passes.

The unit runs `python -m app.cli run` as `whwebhook` with `Restart=always`,
`RestartPreventExitStatus=78`, a start limit, `MemoryMax=600M`, `ProtectSystem=strict`, a system-call
filter and further hardening (systemd exposure score 1.3).

### 6.3 Meta and Microsoft

See `docs/setup-meta-whatsapp.md` (Employee system user, permanent token, webhook subscription to
`messages`, Live/Publish) and `docs/setup-microsoft365-email.md` (Entra app, RBAC scope to the shared
mailbox, size limits, DNS state at Network Solutions).

## 7. Verification done

- 121 automated tests: signatures, parser fixtures for every type, phone normalisation and ranking,
  composer (Reply-To, headers, splitting, rebuild commands executed in bash), store and worker
  resilience, CRM client and directory, media download, Graph and SMTP transports, end-to-end through
  a local SMTP sink. CI runs ruff and pytest on every push.
- Three adversarial review rounds (correctness, security, spec, deployment, external APIs); every
  confirmed finding fixed with a regression test.
- Deploy assets: shellcheck clean; `caddy validate`/`adapt` with Caddy 2.11.9; `systemd-analyze verify`.
- Live smoke test through a real Caddy: handshake, signature rejection, de-duplication, 413 above
  4 MB, 404 elsewhere, delivery, dead-letter plus alert, and replay through the CLI.

## 8. Security and privacy

Mandatory signature check (`WA_ALLOW_UNSIGNED=false`); only `/webhook` exposed; TLS by Caddy; SSH only
through IAP with OS Login; no VM service account; hardened unprivileged service; secrets only in the
env file; the verify token is kept out of access logs; phone numbers masked in logs (`+16*****1234`);
message bodies never logged; raw payloads purged after 30 days; sender-controlled text sanitised in
headers, file names and rebuild commands. Optional: Caddy mTLS with Meta's client certificate.
Dependencies pinned; Dependabot watches pip and GitHub Actions.

## 9. Work plan

Done: repository, service, tests, deploy assets, setup guides, reviews (all pushed to `main`).

Remaining, by the owner (see README "Quick start"):
1. Create the Google Cloud account and project, then run `deploy/gcp/provision.sh`.
2. Create the `whatsapphook` A record at Network Solutions.
3. Create the shared mailbox and the Entra app, and assign the RBAC role (Microsoft guide, section 2).
4. Create the Meta system user token (Meta guide, section 2).
5. Run `deploy/gcp/deploy.sh`, fill in `/etc/whatsapp-webhook/env`, restart, run the smoke tests
   (`check-config`, `test-crm`, `test-email`, `meta-check`).
6. Configure the webhook in the Meta App Dashboard and send test messages from a phone.

Out of scope unless requested: replying from email back into WhatsApp, a message archive or UI,
multiple tenants.
