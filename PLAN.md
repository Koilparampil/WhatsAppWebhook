# Plan: WhatsApp Business → Email forwarder with VShipCRM Reply-To lookup

Status: **Approved and in implementation** (plan confirmed 2026-11-09; see the decisions log below).
Date: 2026-11-09

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

A small Python service, running on one Google Compute Engine VM behind Caddy (automatic HTTPS on your domain), that:

1. Receives every inbound WhatsApp Business (Cloud API) message via Meta's webhook.
2. Looks the sender's phone number up in VShipCRM.
   - Match found → the matched record's email becomes the **Reply-To** of the forwarded email.
   - No match / lookup error / no phone number in the payload → the email carries the sender's **WhatsApp display name** (and username/BSUID when present) and explains why no Reply-To was set.
3. Sends the message (text, caption, location, contact cards, interactive replies, reactions, media as attachments…) to your email server over SMTP.

Everything (code, tests, deployment scripts, docs) lives in one new Git repository.

---

## 2. Research findings that shape the design

### 2.1 WhatsApp Cloud API webhooks (Meta)

| Fact | Consequence for us |
|---|---|
| Endpoint verification is a `GET` with `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`; we must echo the challenge when the token matches. | `GET /webhook` handler. |
| Every `POST` carries `X-Hub-Signature-256: sha256=<HMAC-SHA256 of raw body, keyed with the App Secret>`. | Verify on the **raw** request bytes with a constant-time compare; reject with 403 otherwise. |
| Meta retries non-200 deliveries with decreasing frequency (docs say up to 36 h on the generic Graph page and up to 7 days on the WhatsApp page) and duplicates are expected. | Return 200 immediately, do the real work asynchronously, and **de-duplicate on `messages[].id` (wamid)**. |
| Payloads can be up to 3 MB; up to 1000 updates may be batched per POST. | Loop over every `entry[].changes[]`; set Caddy body limit ≥ 4 MB. |
| Outbound delivery receipts (`value.statuses[]`) arrive on the same `messages` webhook field. | Ignore `statuses[]`; only `value.messages[]` entries are "incoming messages". |
| Since April 2026 Meta ships **business-scoped user IDs (BSUID)**: `messages[].from_user_id`, `contacts[].user_id`. With WhatsApp **usernames** rolling out in 2026, `messages[].from` / `contacts[].wa_id` (the phone number) **may be omitted** unless you exchanged messages in the last 30 days or the user is in your contact book. A `contacts[].profile.username` may appear. | Phone number is optional in the parser. CRM lookup runs only when a phone is present; otherwise we fall back to display name + username + BSUID. |
| Media messages carry only a `media id` (+ `mime_type`, `sha256`, optional `filename`/`caption`). To fetch: `GET https://graph.facebook.com/{version}/{media-id}` → short-lived URL (5 min) → `GET url` with `Authorization: Bearer <token>`. Inbound media IDs stay downloadable ≤ 7 days. | Download immediately when processing; attach to the email; needs a permanent **System User access token**. |
| Current Graph API version is **v26.0** (released 2026-07-29). | Version is a config value, default `v26.0`. |
| Meta supports mTLS for webhooks (client cert CN `client.webhooks.fbclientcerts.com`, CA `meta-outbound-api-ca-2025-12.pem`) as an alternative to IP allow-listing. | Optional hardening in Caddy (see §8). |

Inbound message types the parser will handle: `text`, `image`, `audio`, `video`, `document`, `sticker`, `location`, `contacts`, `interactive` (button_reply / list_reply / nfm_reply), `button`, `reaction`, `order`, `request_welcome`, `system`, `unsupported` (with `errors[]`), plus `context` (quoted reply / forwarded) and `referral` (click-to-WhatsApp ads).

### 2.2 VShipCRM API (`https://vship2000-prod-api.azurewebsites.net`)

Read from the OpenAPI 3.0.4 document at `/swagger/v1/swagger.json` (86 paths) and confirmed by probing:

- **Auth:** `POST /api/Auth/login` with `{ "username", "password" }` returns a JWT used as `Authorization: Bearer <token>`. The 200 response body is **not documented** in the spec (schema missing) → the exact field holding the token must be confirmed on first live call. `GET /api/Auth/session-status` returns `{ secondsRemaining, isWarning, token }` and `POST /api/Auth/extend` extends the session → tokens expire; the client must re-login on 401 / near expiry.
- Unauthenticated `GET /api/Customer` → **401** (verified), so a dedicated CRM service account is required.
- **There is no "search by phone" endpoint.** The only search endpoints are for Bookings (`/api/Bookings/search?q=`, `/searchBooking?SearchText=`), which search booking text, not party phone numbers. Party lists are only available as full lists:

| Endpoint (GET) | Phone fields | Email fields | Name fields | Active flag |
|---|---|---|---|---|
| `/api/Customer` | `mobile`, `businessPhone` | `email1..email4` | `companyName`, `contactPerson` | `active` |
| `/api/Shipper` | `mobile`, `businessPhone` | `email1..email4` | `companyName`, `contactPerson` | `active` |
| `/api/NotifyingParty` | `mobile`, `businessPhone` | `email1..email4` | `companyName`, `contactPerson` | `active` |
| `/api/Trucker` | `mobile`, `businessPhone` | `email1..email4` | `companyName`, `contactPerson` | `active` |
| `/api/Consignee` | `mobile`, `businessPhone` | `email`, `email2`, `email3` | `companyName`, `contactPerson` | `isActive` |
| `/api/Agent` | `businessPhone` (response has no `mobile`) | `email` | `companyName`, `contactPerson` | `active` |
| `/api/Exporter` | `mobile`, `businessPhone` | `email` | `name`, `person` | `isActive` |
| `/api/User` (staff) | `mobile`, `deskNumber` | `userName` (looks like the login/email) | `firstName`, `lastName` | `active` |

  All responses use the envelope `{ isSuccess, isFailure, error: { code, name }, value: [...] }`.

- **Consequence:** the service keeps an **in-memory phone → record index** built from the configured list endpoints, refreshed periodically (default every 15 min) and on-demand on a cache miss (rate-limited), and matches locally. Phone formats in the CRM are free text, so numbers are normalised to E.164 with the `phonenumbers` library using a configurable default region, with a digits-suffix fallback.

### 2.3 Google Cloud, minimal footprint

- **Free Tier**: one non-preemptible **e2-micro** (2 shared vCPUs, 0.25 vCPU sustained, 1 GB RAM) per billing account in `us-west1`, `us-central1` or `us-east1`, plus 30 GB-months standard persistent disk and 1 GB/month North-America egress. The fetched free-tier page does not mention the external IPv4 address; Google has charged for in-use external IPv4 since 2024 and announced the free e2-micro keeps a no-cost one, but budget ≈ $3.65/month in case it is billed.
- Expected footprint: Caddy ≈ 30–40 MB RSS, Python app ≈ 70–90 MB RSS, idle CPU ≈ 0. Comfortably inside 1 GB with a small swap file as a safety net.
- Everything runs as two systemd services on Debian (no Docker, no Kubernetes, no load balancer) to keep resources and cost minimal.

### 2.4 Caddy

Caddy 2 from the official APT repository; a 4-line Caddyfile gives automatic Let's Encrypt/ZeroSSL certificates, HTTP→HTTPS redirect and the reverse proxy to `127.0.0.1:8000`. Requires DNS A record → VM static IP and ports 80/443 open.

---

## 3. Architecture

```
 WhatsApp user ──► Meta WhatsApp Cloud API
                          │  HTTPS POST /webhook  (X-Hub-Signature-256)
                          ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ GCE e2-micro (Debian), static IP, firewall tcp/80,443         │
   │                                                               │
   │  Caddy :443  (auto-TLS for wa.yourdomain.tld)                 │
   │     │ only /webhook is proxied; everything else → 404         │
   │     ▼                                                         │
   │  uvicorn+FastAPI  127.0.0.1:8000  (systemd, unprivileged)     │
   │     ├─ GET  /webhook  → verify-token handshake                │
   │     ├─ POST /webhook  → verify HMAC → parse → INSERT OR IGNORE│
   │     │                   into SQLite queue → 200 (fast)        │
   │     └─ background worker (same process)                       │
   │           1. normalise message                                │
   │           2. CRM lookup (in-memory index) ──► VShipCRM API    │
   │           3. download media ───────────────► graph.facebook  │
   │           4. build MIME email (Reply-To if matched)           │
   │           5. SMTP send ────────────────────► your mail server │
   │           6. mark done / retry with backoff / dead-letter     │
   └───────────────────────────────────────────────────────────────┘
```

Why a persistent queue: Meta expects a quick 200; CRM lookup + media download + SMTP can take seconds and any of them can be down. SQLite (stdlib) gives durability across restarts, de-duplication, retries and a dead-letter list, with zero extra services.

---

## 4. Repository layout (proposed name: `whatsapp-email-webhook`)

```
whatsapp-email-webhook/
├── README.md                 setup + runbook (Meta app, DNS, GCP, operations)
├── PLAN.md                   this document
├── pyproject.toml            metadata, deps, ruff + pytest config
├── requirements.txt          pinned runtime deps (used by install.sh)
├── requirements-dev.txt      pytest, respx, aiosmtpd, ruff
├── .env.example              every config variable, documented, no secrets
├── .gitignore
├── app/
│   ├── main.py               FastAPI app, routes, lifespan (start worker, warm CRM cache)
│   ├── config.py             pydantic-settings model (env vars)
│   ├── security.py           verify-token check, HMAC-SHA256 signature check
│   ├── whatsapp/
│   │   ├── models.py         typed models for the webhook payload (phone optional)
│   │   ├── parser.py         payload → list[InboundMessage] (all types, context, referral)
│   │   └── media.py          Graph API media fetch (id → url → bytes, sha256 check, size cap)
│   ├── crm/
│   │   ├── client.py         login / token refresh / list endpoints / envelope parsing
│   │   ├── directory.py      phone→record index, periodic + on-miss refresh
│   │   └── phones.py         E.164 normalisation + suffix fallback + match ranking
│   ├── mail/
│   │   ├── compose.py        subject, text+HTML bodies, headers, attachments
│   │   └── smtp.py           smtplib sender (STARTTLS / SMTPS / plain), timeouts
│   ├── store.py              SQLite queue + dedup (wamid PK) + attempts/backoff
│   ├── worker.py             processing loop, retries, dead-letter, optional alert email
│   └── cli.py                `lookup <phone>`, `test-email`, `test-crm`, `replay-failed`, `purge`
├── tests/                    pytest (see §7) + fixtures/payloads/*.json
├── deploy/
│   ├── Caddyfile
│   ├── systemd/whatsapp-webhook.service
│   ├── install.sh            idempotent on-VM installer (Caddy, venv, user, units, swap, upgrades)
│   └── gcp/
│       ├── provision.sh      gcloud: static IP, firewall, e2-micro VM
│       └── deploy.sh         copy repo to VM + run install.sh (re-runnable for updates)
└── .github/workflows/ci.yml  ruff + pytest on every push/PR
```

Stack: Python 3.13 (Debian 13 ships 3.13; falls back to 3.11 on Debian 12), FastAPI + uvicorn (single worker), httpx, pydantic-settings, `phonenumbers`, stdlib `sqlite3`, `smtplib`, `email`. Tooling: ruff, pytest.

---

## 5. Detailed behaviour

### 5.1 HTTP endpoints (only `/webhook` is reachable through Caddy)

- `GET /webhook` – if `hub.mode == "subscribe"` and `hub.verify_token` equals `WA_VERIFY_TOKEN` → `200` plain-text `hub.challenge`; else `403`.
- `POST /webhook` – read raw body → verify `X-Hub-Signature-256` with `WA_APP_SECRET` (`hmac.compare_digest`) → `403` on mismatch/missing → parse JSON → for each `entry[].changes[]` with `field == "messages"`: for each `value.messages[]`, `INSERT OR IGNORE (wamid, received_at, raw_value_json)` into SQLite → `200 {"status":"queued","accepted":N,"duplicates":M}`. `statuses[]` and other fields are logged at debug level and dropped. Optional `WA_PHONE_NUMBER_ID` filter to ignore other business numbers.
- `GET /healthz` – bound to localhost only (not proxied): queue depth, CRM cache age, last SMTP success.

### 5.2 Worker (one background task, concurrency 1 by default)

For each pending row, oldest first:

1. **Normalise** → `InboundMessage{wamid, type, timestamp, from_phone (opt), bsuid, display_name, username (opt), to_business_number, text, caption, media{id,mime,sha256,filename}, location, contacts, interactive_reply, button, reaction, order, referral, context{quoted_wamid, forwarded}, errors}`.
2. **CRM lookup** (`from_phone` present only): normalise → index lookup → best match → `{entity, id, company, contact_person, matched_field, matched_number, emails[]}`. Outcomes: `matched`, `no_phone`, `no_match`, `error(reason)`. Every non-`matched` outcome ⇒ "lookup failed" ⇒ no Reply-To, display name featured.
3. **Media** (if any): fetch via Graph API, verify size ≤ `WA_MEDIA_MAX_BYTES` (default 20 MB) and the payload `sha256`; on failure after 3 quick retries, send the email anyway with a note ("attachment could not be downloaded: …").
4. **Compose email** (§5.4).
5. **Send** via SMTP (run in a thread so the event loop stays free).
6. **Finish**: mark `done`; on exception schedule retry with backoff (30 s, 2 m, 10 m, 30 m, 1 h, 3 h, 6 h … capped at 24 h) up to `WORKER_MAX_ATTEMPTS` (default 20), then mark `failed` (dead-letter), log an error and optionally send a short alert email (`ALERT_ON_DEAD_LETTER=true`). `cli.py replay-failed` re-queues. Processed rows are purged after `STORE_RETENTION_DAYS` (default 30; raw payload kept only until then).

### 5.3 CRM lookup details

- **Login**: `POST /api/Auth/login`; token field detected from the response (`token` / `accessToken` / bare string – confirmed on first live run and then pinned). Cached in memory; refreshed when the JWT `exp` claim is < 60 s away or on any 401 (one re-login + retry per request).
- **Index build**: `GET` each endpoint in `CRM_ENTITIES` (default `Customer`; can be `Customer,Shipper,Consignee,NotifyingParty,Trucker,Agent,Exporter,User`), keep `active`/`isActive` records (unless `CRM_INCLUDE_INACTIVE=true`), normalise every phone field (`mobile` first, then `businessPhone`) with `phonenumbers` and `CRM_DEFAULT_REGION` (e.g. `US`); index by E.164 and by last-9-digit suffix.
- **Refresh**: background every `CRM_REFRESH_SECONDS` (default 900). On a miss, if the index is older than 60 s, refresh synchronously once and retry (so a customer added a minute ago is still found). If the CRM is unreachable the stale index keeps serving (with its age noted in the email).
- **Ranking** when several records share a number: exact E.164 > suffix match; entity order as listed in `CRM_ENTITIES`; `mobile` over `businessPhone`; active over inactive; most recently updated. Ties → first, and the email notes "N other CRM records share this number".
- **Reply-To**: first non-empty, syntactically valid email of the matched record (`email1→email4` or `email→email3`). Option `CRM_REPLY_TO_ALL_EMAILS=true` puts all of the record's addresses in Reply-To.

### 5.4 Email format

- `From`: `"<display name> via WhatsApp" <MAIL_FROM>` – the mailbox must be fixed (SPF/DKIM/DMARC), the display-name part carries the WhatsApp name. Template configurable.
- `To`: `MAIL_TO` (one or more).
- `Reply-To`: CRM email(s) when matched; otherwise header omitted (optionally `MAIL_FALLBACK_REPLY_TO`).
- `Subject`: `[WhatsApp] Sheena Nelson (+1 650-555-1234): Does it come in another color?` (text preview ≤ 80 chars) or `[WhatsApp] Sheena Nelson sent an image`. Prefix configurable.
- Body (text/plain + HTML alternative):
  - Sender block: display name, phone (or "phone not shared by WhatsApp"), username, BSUID, local time (`MAIL_TIMEZONE`), business number it was sent to, message type, wamid.
  - Content: text / caption / location (with Google Maps link) / contact cards / interactive reply (title + id) / button payload / reaction (emoji + referenced message) / order summary / unsupported-type notice with Meta's error text / "replying to …" and "forwarded" context.
  - CRM block: **Matched**: `Customer #123 – Acme Imports (John Doe) – mobile 650-555-1234 – replies go to john@acme.com`; **Not matched**: `No VShipCRM record for +1 650-555-1234 (searched Customer, Shipper…)`; **Error**: `VShipCRM lookup failed: <reason> – index age 2h`.
- Attachments: media with original `filename` or `{type}-{wamid}.{ext}` derived from MIME; stickers (webp) and voice notes (ogg) included.
- Threading/filter headers: deterministic `Message-ID: <{wamid}@{MAIL_FROM domain}>` (mail-side dedup), `In-Reply-To`/`References` set from `context.id` so quoted replies thread in the mail client, plus `X-WhatsApp-Message-Id`, `X-WhatsApp-From`, `X-WhatsApp-BSUID`, `X-WhatsApp-CRM-Match: customer:123|none|error`.
- Note: this is one-way. Replying to the email goes to the CRM contact's mailbox, not back into WhatsApp.

### 5.5 Configuration (environment file `/etc/whatsapp-webhook/env`, mode 0600)

```
# WhatsApp / Meta
WA_VERIFY_TOKEN=            # random string you also paste into the Meta App Dashboard
WA_APP_SECRET=              # Meta App → Settings → Basic → App Secret (signature check)
WA_ACCESS_TOKEN=            # permanent System User token (media download)
WA_GRAPH_API_VERSION=v26.0
WA_PHONE_NUMBER_ID=         # optional: only process this business number
WA_MEDIA_MAX_BYTES=20000000
# VShipCRM
CRM_BASE_URL=https://vship2000-prod-api.azurewebsites.net
CRM_USERNAME= / CRM_PASSWORD=
CRM_ENTITIES=Customer
CRM_DEFAULT_REGION=US
CRM_REFRESH_SECONDS=900
CRM_INCLUDE_INACTIVE=false
CRM_REPLY_TO_ALL_EMAILS=false
CRM_TIMEOUT_SECONDS=20
# Mail
SMTP_HOST= / SMTP_PORT=587 / SMTP_SECURITY=starttls|ssl|none
SMTP_USERNAME= / SMTP_PASSWORD=
MAIL_FROM=whatsapp@yourdomain.tld
MAIL_TO=ops@yourdomain.tld[,more]
MAIL_SUBJECT_PREFIX=[WhatsApp]
MAIL_TIMEZONE=America/New_York
MAIL_FALLBACK_REPLY_TO=     # optional
ALERT_ON_DEAD_LETTER=true
# App
APP_BIND=127.0.0.1:8000
STORE_PATH=/var/lib/whatsapp-webhook/queue.db
STORE_RETENTION_DAYS=30
WORKER_MAX_ATTEMPTS=20
LOG_LEVEL=INFO
```

---

## 6. Deployment

### 6.1 GCP provisioning (`deploy/gcp/provision.sh`, run from your workstation with `gcloud` logged in)

1. Enable Compute Engine API in the chosen project.
2. Reserve a regional static external IPv4 address.
3. Firewall rule: allow `tcp:80,443` from anywhere to network tag `whatsapp-webhook`; allow `tcp:22` only from IAP range `35.235.240.0/20` (SSH via `gcloud compute ssh --tunnel-through-iap`).
4. Create VM: `e2-micro`, zone `us-central1-a` (free tier; configurable), image family `debian-13` (fallback `debian-12`), 10 GB `pd-standard`, Shielded VM, OS Login on, no service-account scopes, network tag above, the static IP.
5. Print the IP and the DNS record to create: `A  wa.yourdomain.tld → <ip>` (manual, or scripted if the zone is in Cloud DNS).

### 6.2 On-VM install (`deploy/gcp/deploy.sh` → copies the repo and runs `deploy/install.sh` as root; idempotent, re-run for updates)

- `apt` basics, `unattended-upgrades`, 1 GB swap file, journald size cap.
- Caddy from the official repo; `/etc/caddy/Caddyfile`:
  ```
  wa.yourdomain.tld {
      request_body { max_size 4MB }
      handle /webhook { reverse_proxy 127.0.0.1:8000 }
      handle { respond 404 }
      log { output stderr; format json }   # journald
  }
  ```
- System user `whwebhook`; code in `/opt/whatsapp-webhook` with a venv; env file `/etc/whatsapp-webhook/env` (created from `.env.example` on first install, never overwritten); data dir `/var/lib/whatsapp-webhook`.
- systemd unit: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1`, `Restart=always`, hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `ReadWritePaths=/var/lib/whatsapp-webhook`, `MemoryMax=400M`).
- Smoke test at the end: `curl -s https://wa.yourdomain.tld/webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=123` → `123`.

### 6.3 Meta side (documented step-by-step in README)

Meta app with WhatsApp product → business phone number → System User with `whatsapp_business_messaging` + `whatsapp_business_management` and a permanent token → App Dashboard → WhatsApp → Configuration → Callback URL `https://wa.yourdomain.tld/webhook` + Verify Token → subscribe to the **`messages`** field.

---

## 7. Testing

- **Unit** (pytest): signature verification (good/bad/missing header, raw-bytes correctness); verify handshake; parser fixtures for every message type incl. a username-only message with no phone, `statuses`-only payload, batched payload, unsupported type with errors; phone normalisation (US formats, `+44`, `00` prefix, extensions, garbage) and match ranking; email composition (Reply-To present/absent, From display name, subject truncation, attachments, threading headers, HTML escaping); SQLite dedup, backoff schedule, dead-letter; CRM client (login, token refresh on 401, envelope errors, pagination-less lists) with `httpx.MockTransport`.
- **Integration**: FastAPI `TestClient` + mocked Graph media + mocked CRM + local `aiosmtpd` sink → post a signed image message from a CRM-known number → assert one email with the right Reply-To and attachment; post the same wamid twice → one email.
- **Live verification checklist** (after deploy): handshake, send text from a phone that exists in the CRM (Reply-To set), from an unknown phone (display name, no Reply-To), an image, a quoted reply (threading), check `journalctl -u whatsapp-webhook`, `cli.py lookup +16505551234`.
- **CI**: GitHub Actions – `ruff check`, `ruff format --check`, `pytest` on push and PR.

---

## 8. Security & privacy

- HMAC signature mandatory in production (`WA_ALLOW_UNSIGNED=false`); verify token; only `/webhook` exposed; TLS by Caddy; firewall 80/443 only, SSH via IAP; unprivileged hardened service; secrets only in the 0600 env file (never in git; `.env.example` has placeholders); tokens never logged; phone numbers masked in INFO logs (`+1650***1234`); SQLite payloads purged after retention.
- Optional hardening (your call): Caddy mTLS requiring Meta's client certificate (`client.webhooks.fbclientcerts.com`), which blocks any non-Meta caller before the app sees the request.
- Dependencies pinned; Dependabot config included.

---

## 9. Work plan (after your go-ahead)

1. Create the repo + scaffold + CI (empty green pipeline).
2. Webhook endpoints, signature check, SQLite queue/dedup – with tests.
3. Payload parser for all message types – with fixture tests.
4. CRM client, phone normalisation, directory index, ranking – with tests (live login response shape confirmed with your service account).
5. Mail composer + SMTP sender – with aiosmtpd tests.
6. Worker (retries, dead-letter, alerts), CLI tools, `/healthz`.
7. Deploy assets (Caddyfile, systemd, install.sh, GCP scripts) + README runbook.
8. Live rollout: provision VM, DNS, cert, Meta webhook subscription, end-to-end tests from §7.

Out of scope unless you ask: replying from email back into WhatsApp, message archive/UI, multiple tenants.
