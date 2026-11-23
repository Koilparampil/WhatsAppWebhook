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
wh relay-check           # WhatsApp updates from email: see the section of that name below
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
4. Replaying also re-queues failed WhatsApp updates (keys `relay:...`, shown by `wh list-failed` as
   `kind=relay`). A replay never sends a customer anything twice: every send of an update is recorded
   before and after it happens. What a replay does depends on when the update was given up, which the
   alert `A WhatsApp update email could not be finished` (next section) says:
   - It says "Nothing more will be sent for this email": the service had read the email and checked its
     sender (an allowed one) when it gave up (for example because WhatsApp, or Meta's template list, could
     not be reached for longer than `WORKER_RETRY_WINDOW_SECONDS`). Whatever had not gone out by then
     was marked as not sent, and the alert says whether the employee was told the outcome; for an
     update that did not go out they were told "NOT sent ... It will not be sent later on its own", so
     they send it again if it is still needed. A replay only finishes the bookkeeping, such as moving
     the email to `RELAY_FOLDER`: it never sends anything to WhatsApp, however old the email is, and
     never a second outcome email.
   - It says "The email had not been read yet" (the mailbox could not be read): a replay handles it like
     a new email: sent if it is less than `RELAY_MAX_AGE_HOURS` old, otherwise refused with a notice to
     the employee.
   - It says "It was not an update command": nothing was sent, and a replay does nothing more.
   - It says "Do not replay it" (its saved state cannot be read, or could not be written): don't; do
     what the alert says.
   - There is no alert because the update was given up while this feature was not running: by 570fd03
     after a rollback (`wh list-failed` shows `unknown queue item kind 'relay'`), or with the log line
     `dead-lettered relay:... while WhatsApp updates are switched off: its sender was not told`. (While
     the feature is merely switched off, this version gives up no update, not even one that keeps
     crashing the service: it waits until the feature is on again.) A replay, with the feature on,
     carries on where the update stopped. Nothing that was being sent then is repeated; the steps not
     tried yet go through the same checks as every send (an email now more than `RELAY_MAX_AGE_HOURS`
     old is refused, with a notice to the employee).

## WhatsApp updates from email

Only when `RELAY_ENABLED=true` (setup: [whatsapp-updates-setup.md](whatsapp-updates-setup.md)).

```bash
wh relay-check            # mailbox access, templates Approved + Utility, sending number, messaging limit
wh relay-check --sample   # also: how the newest command email was read, and Exchange's internal mark
nano /tmp/body.txt        # paste an email's text (Name: ... ##) to try the rules offline, then:
wh relay-parse --subject 'WA | 650 555 1234 | BK1' --body-file /tmp/body.txt
rm /tmp/body.txt          # it may hold customer details
```

- `relay-parse` sends nothing. Give `--body-file` the file's full path, in a folder the service user can
  read such as `/tmp`: the wrapper runs the command as `whwebhook` from `/opt/whatsapp-webhook`, so a
  plain `body.txt` is looked for there and is not found (`cannot read body.txt: [Errno 2] ...`). Add
  `--html` when the file holds an email's HTML (an email saved from Outlook as HTML). Exit code 0: the
  email would be taken; 2: it would be refused, or is not a command; 1: the file cannot be read.
- `/healthz` has a `relay` block: whether the feature is on, dry run or test mode, when the Inbox was last
  read, the last error and since when reading fails. If it shows `"enabled": false` with an `"error"`
  although `RELAY_ENABLED=true`, the feature did not start (for example `not started, settings: ...`): a
  mistake in a `RELAY_*` setting never stops the service or the forwarder, it only leaves this feature off.
  The same error shows while the feature is off when a `RELAY_*` value cannot be read at all (such as
  `RELAY_ENABLED=ture`, which keeps the feature off; the startup line in the log then says
  `relay=off (a RELAY_* setting cannot be read)`). `wh check-config` prints such problems as `WARNING`
  lines and `wh relay-check` as `FAIL` lines; fix the setting, then
  `wh check-config > /dev/null && sudo systemctl restart whatsapp-webhook`.
- Handled command emails are in the folder **WhatsApp updates - handled** (`RELAY_FOLDER`) of the relay
  mailbox; command-shaped emails from senders not on the list stay in its Inbox.
- Each decision is logged in one line, never with the email's text (phone numbers masked). `#12` is the
  queue row; the word in brackets after `refused` is the reason code:

  ```
  relay #12 from a@vship2000.com: sending to +16*****1234, 1 of 1 file(s) to send
  relay #12 from a@vship2000.com: dry run to +16*****1234, 0 of 0 file(s) to send
  relay #12 from a@vship2000.com: refused (phone_invalid)
  relay #12 from x@example.com: not accepted (the sender is not on RELAY_ALLOWED_SENDERS); left in the Inbox
  relay #12: not a command (the subject does not start with WA |); left in the Inbox
  relay #12: the email no longer exists; nothing to do
  ```

  What happens next is logged the same way (`relay #12: update sent to +16*****1234 (wamid...)`,
  `relay #12: template may or may not have been sent (...)`,
  `relay #12: WhatsApp refused template (code ...)`). All of these lines:
  `journalctl -u whatsapp-webhook | grep 'relay #'`.
- Emails to `ALERT_TO` about this feature, and what to do. `[WhatsApp]` is `MAIL_SUBJECT_PREFIX`; the
  address is the relay mailbox's:

  | Subject | Meaning and what to do |
  |---|---|
  | `[WhatsApp] ALERT: Emails from senders who may not send WhatsApp updates` | Command-shaped emails were ignored and left in the Inbox, because the sender is not on `RELAY_ALLOWED_SENDERS`, Exchange did not mark the email as sent by a signed-in internal user, it was sent on behalf of someone else, its subject was edited in the mailbox after it arrived, or it is a draft written in the mailbox (someone with write access to it may be trying to send WhatsApp messages: check who has access, setup guide section 4.3). The email lists each one with its reason; at most one such email per 24 h (the first at once, later ones collected for the next). Add the person to `RELAY_ALLOWED_SENDERS` if they should send updates; otherwise nothing to do |
  | `[WhatsApp] ALERT: WhatsApp template vship_update cannot be used` (or the booking or document template's name) | Nothing is sent with that template; the email gives Meta's reason (not Approved, not Utility, a different number of variables, not found). It also comes when Meta refuses a send because the template was paused, disabled, changed or withdrawn after the last check (WhatsApp error 132000, 132001, 132015 or 132016). Employees who send an update are told it was NOT sent (`the WhatsApp template is not ready`), or, for the document template, that the file was not sent. Check the template in WhatsApp Manager, then `wh relay-check`. At most one per template per 24 h |
  | `[WhatsApp] ALERT: WhatsApp updates: the Inbox of whatsapp@vship2000.com cannot be read` | The mailbox could not be read for 15 minutes; commands wait in the Inbox. Check Graph permissions (`wh relay-check`) |
  | `[WhatsApp] ALERT: WhatsApp updates: the Inbox of whatsapp@vship2000.com can be read again` | The outage is over; the waiting commands are handled now (those more than `RELAY_MAX_AGE_HOURS` old by then are refused, with a notice to their senders). Nothing to do |
  | `[WhatsApp] ALERT: A WhatsApp update email could not be finished` | An update email was given up after repeated errors. The alert says what did and did not go out and whether the employee was told; an employee told that the update did not go out was asked to send it again (or contact the customer another way) if it is still needed. Fix the cause. If the alert says "Do not replay it", don't. Otherwise nothing more is sent for the email, not even after a replay, unless the alert says it had not been read yet. The alert does not name the queue key: `wh list-failed` shows it (`relay:...`); `wh replay-failed <key>` then only files the email, or handles a not yet read one like a new email (step 4 of "Delivery failures" above) |

- Switch off: `RELAY_ENABLED=false`, then
  `wh check-config > /dev/null && sudo systemctl restart whatsapp-webhook`. Command emails stay in the
  Inbox; when switched on again, those less than `RELAY_MAX_AGE_HOURS` old are handled, older ones are
  refused with a notice.

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

The VM is created with deletion protection. To delete or rebuild it, first run
`gcloud compute instances update whatsapp-webhook --zone us-east1-b --no-deletion-protection`.
Keep the reserved static IP; a new VM can be created with the same address so DNS and the Meta
webhook URL stay valid.

## Logs and privacy

Application logs go to journald (`journalctl -u whatsapp-webhook`). Phone numbers are masked
(`+16*****1234`) when `LOG_MASK_PHONES=true`. Message bodies are never logged. Raw webhook payloads
are stored in the queue database until the row is purged (`STORE_RETENTION_DAYS`).

## Resource footprint

Expected steady state on the e2-micro: Caddy 30 to 40 MB, the Python service 70 to 100 MB, CPU near
zero. While a large file is split and sent through Graph the service peaks around 170 MB at the
default 32 MB limit (measured with a 100 MB document); the SMTP fallback peaks around 330 MB.
`MemoryMax=600M` in the unit file stops a runaway process from taking the VM down; a 1 GB swap file
is created by `install.sh` as a safety net.

Disk: the queue database stays small (raw webhook payloads, purged after `STORE_RETENTION_DAYS`).
Media is spooled to `/var/lib/whatsapp-webhook/spool` only while a message is being processed, so
the largest file in flight (WhatsApp allows documents up to 100 MB) is the only disk peak.
