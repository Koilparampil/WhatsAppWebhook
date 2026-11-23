# WhatsApp updates from email: what to change, step by step

Branch `sendUpgrade`. This guide lists **every** change needed outside the code (Meta, Microsoft 365, the
VM) to switch on the new feature, in the order to do them, with a test after each stage and a way back.
Written 19 Nov 2026 against Meta's and Microsoft's documentation of that date (sources at the end).

**What the feature does.** An employee whose address is on a fixed list emails
`whatsapp@vship2000.com` with the subject `WA | <phone number> | <booking number>` and a body of

```
Name:<customer's name>
Message:
<the update>
##
```

The service sends the customer the approved WhatsApp template

```
Hello {{1}}, VShip has an update:
{{2}}
Please either reply to this message or send us an email at Elizabeth@vship2000.com if you have any Questions
```

with `{{1}}` = Name and `{{2}}` = Message, and emails the employee what happened. **Nothing about replies
changes**: when the customer answers on WhatsApp, the forwarder emails the answer to `MAIL_TO` with the
customer's VShipCRM addresses as Reply-To, exactly as deployed at commit 570fd03.

The feature is **off** until `RELAY_ENABLED=true`, and when switched on it starts in **dry-run** (checks
everything, sends nothing to WhatsApp).

Contents

1. [Before you start: who does what, and what to have at hand](#1-before-you-start)
2. [Stage 0: find out what is live today (no changes)](#2-stage-0-find-out-what-is-live-today)
3. [Stage 1: Meta (WhatsApp Manager, Billing, App Dashboard)](#3-stage-1-meta)
4. [Stage 2: Microsoft 365 (Exchange Online)](#4-stage-2-microsoft-365)
5. [Stage 3: deploy the code with the feature off](#5-stage-3-deploy-the-code-with-the-feature-off)
6. [Stage 4: switch on in dry-run and test](#6-stage-4-switch-on-in-dry-run)
7. [Stage 5: test numbers only, real messages](#7-stage-5-test-numbers-only)
8. [Stage 6: go live](#8-stage-6-go-live)
9. [Later: the booking template and the document template](#9-later-the-booking-and-document-templates)
10. [Rollback](#10-rollback)
11. [Settings reference](#11-settings-reference)
12. [Decisions this is built on](#12-decisions-this-is-built-on)
13. [Known limits and what only a live test can prove](#13-known-limits)
14. [Sources](#14-sources)

The one-page guide for employees is [whatsapp-updates-staff.md](whatsapp-updates-staff.md).

---

## 1. Before you start

| Who | Needs | For |
|---|---|---|
| Meta admin | Full control of the VShip business portfolio in Meta Business Suite (or "manage" permission on the WhatsApp account **and** the Messaging account, plus finance permission for billing) | Stage 1 |
| Microsoft 365 admin | Global Administrator, or Exchange Administrator; Exchange Online PowerShell on a Windows PC (`Install-Module ExchangeOnlineManagement`) | Stages 0 and 2 |
| VM operator | `gcloud` signed in as the project owner, IAP SSH to the VM, Git Bash on the Windows PC (the deploy script is bash) | Stages 0, 3 to 6 |

Have at hand: the list of employees' email addresses that may send updates, one or two personal phones
with WhatsApp to act as test customers, and 30 minutes to 2 hours of slack for Microsoft permission
changes to take effect.

Values used below (all already used by the deployed forwarder; replace them if yours differ):

| Value | Meaning |
|---|---|
| `whatsapp@vship2000.com` | the shared mailbox the forwarder sends from; it now also receives the commands |
| `<Enterprise app Object ID>` | Object ID under **Entra ID > Enterprise applications > WhatsApp Webhook Mailer** (not the one under App registrations) |
| `<WABA ID>` | the WhatsApp Business Account id. Meta now calls it the **Messaging account ID**; it is the same number |
| `<PHONE NUMBER ID>` | the business phone number's id (WhatsApp Manager > Phone numbers, or App Dashboard > WhatsApp > API Setup) |

---

## 2. Stage 0: find out what is live today

Nothing is changed in this stage. It decides which Microsoft 365 steps of stage 2 (section 4) are needed.

### 2.1 On the VM

```bash
gcloud compute ssh whatsapp-webhook --zone us-east1-b --tunnel-through-iap
whatsapp-webhook check-config | grep -E '"(mail_transport|graph_sender|mail_from|mail_to|alert_to|wa_phone_number_id|wa_waba_id|mail_fallback_reply_to)"'
sudo ls -l /opt/whatsapp-webhook/app/
```

Write down:

- `mail_transport`. It **must** be `"graph"` for the new feature: the Inbox is read through Microsoft
  Graph. If it says `"smtp"`, section 4.2 moves the forwarder to Graph (the owner's decision for that case).
- `graph_sender` / `mail_from`. Both normally `whatsapp@vship2000.com`.
- `wa_waba_id` becomes **required**; if empty, section 3.2 finds it. `wa_phone_number_id`: note whether
  it is empty. Its meaning does not change (forward only that number's messages); the sending number goes
  into the new `RELAY_PHONE_NUMBER_ID` (stage 3, 5.3).
- `mail_to` and `alert_to`. `alert_to` receives the new feature's alerts (empty = `mail_to`).

If `/opt/whatsapp-webhook/app/` holds a `replies` folder, the VM runs the undeployed staff-replies work,
not 570fd03. Stop and decide with the owner before going further: this guide assumes 570fd03.

### 2.2 In Exchange Online PowerShell (administrator's PC)

```powershell
Install-Module -Name ExchangeOnlineManagement      # once per PC
Connect-ExchangeOnline -UserPrincipalName <admin>@vship2000.com

# The rights Exchange has configured for the app on the mailbox (read without the 30 min - 2 h cache;
# live calls may lag behind a recent change by up to 2 hours):
Test-ServicePrincipalAuthorization -Identity <Enterprise app Object ID> -Resource whatsapp@vship2000.com | Format-Table
```

The output has one row per role assignment. The row whose `GrantedPermissions` include `Mail.ReadWrite`
(role "Application Mail Full Access" or "Application Mail.ReadWrite") must show `True` under `InScope`.
"Application Mail.Send" alone is not enough. If the command says the service principal is not found,
Exchange RBAC was never set up for the app (normal for "Model B", below).

Then check in the Entra admin center: **Entra ID > App registrations > WhatsApp Webhook Mailer > API
permissions**. If `Mail.ReadWrite` (Application) is listed there with admin consent, the app uses
"Model B" (rights on every mailbox). That also works, and `Test-ServicePrincipalAuthorization` does not
show it (it only knows Exchange's own assignments).

Things on the mailbox that could hide or change command emails before the service sees them:

```powershell
# People with Full Access. They can read, move or delete commands and the customer data in them, and they
# can send customers WhatsApp messages in the name of any allowed employee by editing an email (section 4.3)
Get-EXOMailboxPermission -Identity whatsapp@vship2000.com |
  Where-Object { $_.AccessRights -match 'FullAccess' -and -not $_.IsInherited -and $_.User -ne 'NT AUTHORITY\SELF' } |
  Format-Table User,AccessRights

# People with rights on the Inbox folder itself (edit rights there allow the same)
Get-EXOMailboxFolderPermission -Identity whatsapp@vship2000.com:\Inbox | Format-Table User,AccessRights

# Inbox rules (a rule that moves or deletes mail would make commands disappear)
Get-InboxRule -Mailbox whatsapp@vship2000.com -IncludeHidden | Format-List Name,Enabled,Priority,Description

# Automatic replies on the mailbox itself (would send each employee one automatic reply per period)
Get-MailboxAutoReplyConfiguration -Identity whatsapp@vship2000.com | Format-List AutoReplyState

# Forwarding (mail must stay in the mailbox)
Get-Mailbox whatsapp@vship2000.com | Format-List ForwardingSmtpAddress,ForwardingAddress,DeliverToMailboxAndForward

# Connectors that can make outside mail count as "internal" (the service's spoofing guard relies on
# Exchange's internal mark; such a connector would weaken it)
Get-InboundConnector | Format-List Name,Enabled,ConnectorType,TreatMessagesAsInternal,CloudServicesMailEnabled,SenderDomains
```

Expected: no Inbox rules that move or delete mail, `AutoReplyState : Disabled`, no forwarding, no
enabled inbound connector of type `OnPremises` (`TreatMessagesAsInternal` and `CloudServicesMailEnabled`
both `False`), nobody with Full Access who is not on `RELAY_ALLOWED_SENDERS` (preferably nobody at all),
and on the Inbox nobody but `Default` and `Anonymous`, both `{None}` (Exchange's default). If any of these
differ, fix it in section 4.3 or note it for the owner.

---

## 3. Stage 1: Meta

### 3.1 Payment method (required before any template can be sent)

Meta charges per delivered template message. On a production account without a payment method, sends fail
with error 131042 ("business eligibility payment issue"; Meta implies this for production accounts and does
not say whether the error comes back at once or later in a delivery report).

1. Open **Meta Business Suite** with the VShip business portfolio selected.
2. Open **Billing Hub**: https://business.facebook.com/latest/billing_hub/payment_methods/ (or from
   WhatsApp Manager: menu > **All tools** > **Billing & payments**).
3. Choose the **Messaging account** (the WhatsApp Business Account) that the business number sends through,
   and add a payment method (credit or debit card) if none is shown.
4. Check that the account's **currency** and **time zone** are set. Currency cannot be changed later.

Meta's help article for this is "About billing for your WhatsApp Business account". Meta's documentation
does not spell out the exact button labels for Messaging accounts; if the screens differ, follow Billing
Hub's own wording.

Cost, for budgeting: since 1 Oct 2026 every Utility template message is charged, also inside an open
24-hour window. Secondary sources give about USD 0.0034 per Utility message to a US number; the binding
figure is the USD rate card in WhatsApp Manager / Billing Hub. Files sent inside the 24-hour window are
"service" messages (1,000 free per business number per month, then charged).

### 3.2 Find the two ids the service needs

| Setting | Where |
|---|---|
| `RELAY_PHONE_NUMBER_ID` | App Dashboard > **WhatsApp** > **API Setup** ("Phone number ID" of the business number) |
| `WA_WABA_ID` | App Dashboard > **WhatsApp** > **API Setup** ("WhatsApp Business Account ID" / "Messaging account ID") |

Once `WA_WABA_ID` is set, `whatsapp-webhook meta-check` on the VM lists the account's business numbers with
their ids (`number: id=...`). It does not print the WABA id itself: take that from API Setup.

### 3.3 Account state that limits sending (check, nothing to change if fine)

After stage 3, `whatsapp-webhook relay-check` prints the messaging limit and the display-name status; the
app mode is shown only in the App Dashboard, and the token's permissions by `whatsapp-webhook meta-check`.
You can also look now:

- **Messaging limit** (WhatsApp Manager > **Account tools** > **Messaging limits**): new portfolios may
  reach **250 different customers per rolling 24 hours** with business-initiated messages. Business
  verification raises it to 2,000. Updates to customers who wrote to VShip in the last 24 hours do not
  count.
- **Display name status** of the number: should be Approved. Meta's current pages do not say an
  unapproved name blocks templates, but customers may see only the number.
- **App mode**: it should be **Live** (published). [setup-meta-whatsapp.md](setup-meta-whatsapp.md),
  section 4, step 5, says where (an **App Mode** toggle, or **Publish** in the left menu). Meta's webhook
  troubleshooting says some
  webhooks are not sent in Development mode, and the feature relies on delivery-report webhooks to tell
  employees about late failures. Your own app needs only Standard access (no App Review): Meta states
  this for businesses using the API for themselves.
- **Token permissions**: the System User token in `WA_ACCESS_TOKEN` must have
  `whatsapp_business_messaging` (sending) and `whatsapp_business_management` (reading templates). The
  existing guide already creates it with both ([setup-meta-whatsapp.md](setup-meta-whatsapp.md),
  section 2).

### 3.4 Create the template `vship_update`

Who: someone with access to the WhatsApp account and the Messaging account in Meta Business Suite.

1. Open WhatsApp Manager: https://business.facebook.com/latest/whatsapp_manager/message_templates
   (or Meta Business Suite > **WhatsApp Manager** > **Account tools** (suitcase icon) > **Message templates**).
2. **Create template**.
3. Category: **Utility**. (Do not choose Marketing: Meta does not deliver Marketing templates to US
   numbers.)
4. Name: `vship_update` (exactly; lower case and underscore).
5. Language: **English (US)** (code `en_US`).
6. No header. **Body**, typed exactly as below, with the two line breaks. Use the editor's "add
   variable" control (or type `{{1}}` and `{{2}}`):

   ```
   Hello {{1}}, VShip has an update:
   {{2}}
   Please either reply to this message or send us an email at Elizabeth@vship2000.com if you have any Questions
   ```

7. No footer, no buttons.
8. Sample values (Meta requires one per variable; never use real customer data):
   `{{1}}` = `Maria`, `{{2}}` = `Your container for booking BK12345 has arrived at the port of Newark.`
9. Submit. Review takes up to 24 hours. Meta emails the result to the portfolio's admins.

**Check the category after approval.** Since April 2025 Meta approves a template it considers marketing
*as Marketing* instead of rejecting it. Open the template in WhatsApp Manager: the category must read
**Utility**. The service checks this itself before every send and refuses to send if the template is not
Approved + Utility (decision 11 in section 12); `relay-check` reports it too.

If Meta files it as Marketing: Meta's Utility rule is that the content must be tied to the customer's
order, account or transaction. A template that names the booking is the obvious fix: that is what the
booking template of section 9 is for. You can ask Meta for a category review within 60 days of the
template's creation or category change: WhatsApp Manager > **Message Templates** > **Message Templates** >
the banner's **Go to Business Support** > **Template Category Updates** > select the template >
**Request Review**.

The same template can be created through the API instead of the screens. Run this on the VM (or any
computer with bash, curl and openssl). It asks for the System User token and the App Secret without showing
them, and adds the `appsecret_proof` that Meta requires when "Require App Secret" is switched on
([setup-meta-whatsapp.md](setup-meta-whatsapp.md), section 5, "Limits and optional hardening"):

```bash
read -rsp 'WhatsApp token: ' WA_ACCESS_TOKEN; echo; read -rsp 'App secret: ' WA_APP_SECRET; echo
PROOF=$(printf %s "$WA_ACCESS_TOKEN" | openssl dgst -sha256 -hmac "$WA_APP_SECRET" | sed 's/^.* //')
curl -s -X POST "https://graph.facebook.com/v27.0/<WABA ID>/message_templates?appsecret_proof=$PROOF" \
  -H "Authorization: Bearer $WA_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "vship_update",
    "language": "en_US",
    "category": "UTILITY",
    "components": [{
      "type": "BODY",
      "text": "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at Elizabeth@vship2000.com if you have any Questions",
      "example": {"body_text": [["Maria", "Your container for booking BK12345 has arrived at the port of Newark."]]}
    }]
  }'
```

The answer shows `"status": "PENDING"` and `"category"`; the category must be `"UTILITY"`.

### 3.5 Customer consent (policy, not a setting)

WhatsApp's Business Messaging Policy (updated 23 Sep 2026) allows business-initiated messages only to
people who gave VShip their number **and** agreed to receive messages from VShip, and requires that any
request to stop (on WhatsApp or elsewhere) is honoured. Make sure booking terms or forms record that
consent. When a customer asks to stop, staff must stop sending updates to that number: the service does
not keep a stop list.

---

## 4. Stage 2: Microsoft 365

### 4.1 Give the app the right to read and move mail in the mailbox (only if stage 0 showed it missing)

Skip this if `Test-ServicePrincipalAuthorization` showed a row with `Mail.ReadWrite` and `InScope True`
for `whatsapp@vship2000.com`, **or** if stage 0 found `Mail.ReadWrite` (Application) with admin consent in
Entra ID ("Model B").

If the app has "Application Mail.Send" only (or nothing), assign "Application Mail Full Access"
(= Mail.ReadWrite + Mail.Send) on the same scope the forwarder already uses:

```powershell
Connect-ExchangeOnline -UserPrincipalName <admin>@vship2000.com

# Only if Exchange does not know the app yet (Test-ServicePrincipalAuthorization said "not found").
# AppId = Application (client) ID; ObjectId = Enterprise application Object ID:
New-ServicePrincipal -AppId <Application (client) ID> -ObjectId <Enterprise app Object ID> -DisplayName "WhatsApp Webhook Mailer"

# If the scope does not exist yet (Get-ManagementScope "WhatsApp relay sender" finds nothing):
New-ManagementScope -Name "WhatsApp relay sender" -RecipientRestrictionFilter "UserPrincipalName -eq 'whatsapp@vship2000.com'"

New-ManagementRoleAssignment -App <Enterprise app Object ID> -Role "Application Mail Full Access" `
  -CustomResourceScope "WhatsApp relay sender"

Test-ServicePrincipalAuthorization -Identity <Enterprise app Object ID> -Resource whatsapp@vship2000.com | Format-Table
```

Live calls can take 30 minutes to 2 hours to follow the change. Nothing else in Entra ID changes: no new
API permission and no new secret.

### 4.2 Only if the VM uses SMTP today

The feature needs `MAIL_TRANSPORT=graph`. The owner decided that in this case the forwarder moves to Graph,
the transport PLAN.md records as the confirmed design. Know what that changes for **forwarded** emails
(setup-microsoft365-email.md, section 2f): an email above about 3.5 MB is sent as a draft, so it gets
Exchange's own Message-ID, at most five `X-WhatsApp-*` headers and no In-Reply-To threading; and Graph
keeps a copy of every email in the mailbox's Sent Items. Then follow [setup-microsoft365-email.md](setup-microsoft365-email.md)
section 2 (2a to 2f) completely, then set on the VM `MAIL_TRANSPORT=graph`, `GRAPH_SENDER=whatsapp@vship2000.com`
and the three `OAUTH_*` values, restart, and run `whatsapp-webhook test-email` before going on.

### 4.3 Clean up the mailbox, if stage 0 found something

```powershell
# Switch off automatic replies on the mailbox itself
Set-MailboxAutoReplyConfiguration -Identity whatsapp@vship2000.com -AutoReplyState Disabled

# Remove an Inbox rule that moves or deletes mail (name from Get-InboxRule). Warning: removing a rule with
# PowerShell also deletes every client-side rule that classic Outlook created on this mailbox.
Remove-InboxRule -Mailbox whatsapp@vship2000.com -Identity "<rule name>"
```

**Write access to the mailbox is the power to send WhatsApp messages.** Anyone with Full Access, edit
rights on the Inbox, or an app with `Mail.ReadWrite` on `whatsapp@vship2000.com` can send customers
WhatsApp messages in the name of any allowed employee, by editing an employee's email in the Inbox. The
service refuses an email whose subject was changed after it arrived, and a draft written in the mailbox
(both stay in the Inbox and are listed in the administrators' alert, as "its subject is not the one it
arrived with" or "it is a draft"), but it cannot detect a changed body, or a copy of a command it
handled before. Remove Full Access and folder edit rights from everyone who is not on
`RELAY_ALLOWED_SENDERS` (preferably from everyone), and give no other app `Mail.ReadWrite` on this
mailbox (the service's own app needs it, section 4.1):

```powershell
Remove-MailboxPermission -Identity whatsapp@vship2000.com -User <user> -AccessRights FullAccess -InheritanceType All
Remove-MailboxFolderPermission -Identity whatsapp@vship2000.com:\Inbox -User <user>
```

Anyone who keeps access must not move, delete or open-and-file command emails (subject `WA | ...`) that
are still in the Inbox: the service reads them within about 30 seconds and files them itself.

### 4.4 What does NOT change in Microsoft 365

- **No Exchange lock-down** of the mailbox (decision 21). Mail from anyone still arrives; the service acts
  only on emails from listed addresses that Exchange marked as sent by a signed-in member of the
  organisation. If you later want Exchange itself to refuse outside mail as well, see section 13.
- No change to plus addressing, DNS, SPF, DKIM or DMARC.
- Microsoft Defender "Safe Links" may rewrite links in employees' emails; the service turns them back
  into the original address before anything reaches a customer. Nothing to configure.

---

## 5. Stage 3: deploy the code with the feature off

### 5.1 Commit the branch (on the administrator's PC)

`deploy/gcp/deploy.sh` ships **only files tracked by git**, from the checkout it is run in. A new file of
the service that is not committed (or at least `git add`ed) would be left out: `deploy.sh` then stops
before uploading anything and lists such files (and `install.sh` on the VM refuses an incomplete copy
too, without touching the running service).

```bash
cd <repository folder>/.claude/worktrees/sendUpgrade   # the sendUpgrade worktree (or any clean checkout of the branch)
git status --short          # everything of the feature must be committed
git log --oneline -1        # note this commit for the rollback section
```

**Always deploy from this worktree** (or another clean checkout of `sendUpgrade`). Never from the main
checkout of the repository while it holds other, uncommitted work: `deploy.sh` packs the files git tracks
in the folder it runs from, so it would ship the wrong code.

Before deploying, the tests must pass (on Windows the two settings make Python use UTF-8 and give it a
time zone database):

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt tzdata
PYTHONUTF8=1 .venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
```

### 5.2 Back up the queue database, then deploy

Right before deploying, on the VM (one minute of downtime; Meta retries webhooks, nothing is lost). The
database runs in SQLite's WAL mode, so the service is stopped first and the `.db-wal` / `.db-shm` files
are copied with it:

```bash
gcloud compute ssh whatsapp-webhook --zone us-east1-b --tunnel-through-iap
sudo systemctl stop whatsapp-webhook
sudo mkdir -p /var/lib/whatsapp-webhook/before-sendUpgrade
sudo sh -c 'cp -a /var/lib/whatsapp-webhook/queue.db* /var/lib/whatsapp-webhook/before-sendUpgrade/'
sudo systemctl start whatsapp-webhook
exit
```

Then, on the administrator's PC, from the sendUpgrade checkout:

```bash
PROJECT_ID=<project> DOMAIN=whatsapphook.vship2000.com ACME_EMAIL=thevshipapp@gmail.com bash deploy/gcp/deploy.sh
```

`install.sh` never changes the existing `/etc/whatsapp-webhook/env`. New settings are added by hand in the
next step.

### 5.3 Add the settings, feature still off

```bash
gcloud compute ssh whatsapp-webhook --zone us-east1-b --tunnel-through-iap
sudo nano /etc/whatsapp-webhook/env
```

Add at the end (replace the `<...>` values; addresses separated by commas):

```
# ---- WhatsApp updates from email (sendUpgrade)
WA_WABA_ID=<WABA ID>
RELAY_PHONE_NUMBER_ID=<PHONE NUMBER ID>
RELAY_ENABLED=false
RELAY_DRY_RUN=true
RELAY_ALLOWED_SENDERS=<employee1>@vship2000.com,<employee2>@vship2000.com
RELAY_TEMPLATE_NAME=vship_update
RELAY_TEMPLATE_LANGUAGE=en_US
RELAY_TEMPLATE_PARAMS=name,message
```

- `WA_WABA_ID`: if the file already has a `WA_WABA_ID=` line, fill in that line instead of adding a
  second one.
- `RELAY_PHONE_NUMBER_ID`: the business number the updates are sent from. **Do not** use
  `WA_PHONE_NUMBER_ID` for this if it is empty today: setting `WA_PHONE_NUMBER_ID` also makes the
  forwarder ignore WhatsApp messages sent to any *other* business number of the account, which would
  change the deployed forwarding. If `WA_PHONE_NUMBER_ID` is already set, `RELAY_PHONE_NUMBER_ID` may be
  left out (the same number is used), and if both are set they must be equal (otherwise customers'
  answers to the updates would not be forwarded; the feature then does not start).

Every other relay setting has a default that matches the owner's decisions; see section 11.

After **every** edit of the env file, check it before restarting. `check-config` reads the edited file
without touching the running service; it fails (and then nothing is restarted) only for mistakes that
would stop the whole service. Mistakes in the `RELAY_*` settings never stop the service or the forwarder:
they leave only the WhatsApp updates feature off. `check-config` prints them as `WARNING` lines, but all
of them only once `RELAY_ENABLED=true` (6.2). While the feature is off, as here, it warns only about a
value it cannot read at all (such as `RELAY_DRY_RUN=ture`), so a missing `WARNING` line proves nothing
yet: an address in `RELAY_ALLOWED_SENDERS` that is not an email address, a wrong template setting and
the like are then listed only by `whatsapp-webhook relay-check` (6.1), as `FAIL  setting: ...` lines.

```bash
whatsapp-webhook check-config > /dev/null && sudo systemctl restart whatsapp-webhook
sudo journalctl -u whatsapp-webhook -n 20 --no-pager
whatsapp-webhook check-config | grep relay_
```

The forwarder must behave exactly as before (send yourself a WhatsApp message and check the email
arrives). Nothing reads the mailbox yet.

---

## 6. Stage 4: switch on in dry-run

### 6.1 Check everything from the VM

```bash
whatsapp-webhook relay-check
```

Every line of its report starts with `ok`, `WARN` or `FAIL`. Lines of the service's own log, with a date
and time in front (for example
`... WARNING app.relay.templates: template check for vship_update (en_US): ...`), may appear among them;
they repeat a problem that a `FAIL` line names. While the feature is still off, `relay-check` checks
everything anyway, starts with
`WARN  RELAY_ENABLED is false: the feature is off (checking everything anyway)` and ends with exit code
1; that is expected here. Go on only when no line says `FAIL`.

| Line | Meaning and fix |
|---|---|
| `FAIL  setting: ...` | A setting the feature needs is missing or wrong; the line says which. With the feature on, the service then runs the forwarder only. |
| `FAIL  ... not checked: ...` | A setting that this check needs is missing or wrong (the line names it), so the check was not made. |
| `FAIL  mailbox whatsapp@vship2000.com cannot be read: ...` | The app lacks Mail.ReadWrite on the mailbox, or a change has not taken effect yet (section 4.1; wait up to 2 h). |
| `FAIL  folder "WhatsApp updates - handled" cannot be found or created in whatsapp@vship2000.com: ...` | Same cause (creating the folder needs Mail.ReadWrite). |
| `FAIL  template vship_update (en_US), the update template: WhatsApp template vship_update (en_US) cannot be used. Its text has N variables, but the settings fill M (...)` | The approved body has a different number of `{{}}` than `RELAY_TEMPLATE_PARAMS` lists (for the booking or document template, which the line names instead: its own `..._PARAMS` setting). Fix the setting or the template. |
| `FAIL  template vship_update (en_US), the update template: ...` with any other reason | Not found in `en_US`, not Approved yet, or not Utility; the line says which, and WhatsApp Manager shows it too. `could not be checked` means Meta could not be asked: run `relay-check` again later. |
| `FAIL  WhatsApp ...` | Token or phone number id problem; run `whatsapp-webhook meta-check`. |
| `WARN  messaging limit: ...` | Informational: how many different customers per 24 h can be messaged. |

### 6.2 Switch on

```bash
sudo nano /etc/whatsapp-webhook/env      # set RELAY_ENABLED=true (keep RELAY_DRY_RUN=true)
whatsapp-webhook check-config > /dev/null && sudo systemctl restart whatsapp-webhook
whatsapp-webhook relay-check             # now: no FAIL line, exit code 0
sudo journalctl -u whatsapp-webhook -f
```

The startup line in the log names the relay's state; if it says the relay was not started, the reason is
in the line above it and in `relay-check`.

From the moment it is switched on for the first time, the service handles command emails that arrive
**from then on**. Emails already in the Inbox are never handled.

### 6.3 Prove the internal-sender mark is visible (one time)

From an employee mailbox on the list, send a correct command (any test number) to
`whatsapp@vship2000.com`. Then on the VM:

```bash
whatsapp-webhook relay-check --sample
```

It reads the newest command email from a listed sender and prints whether Exchange's
`X-MS-Exchange-Organization-AuthAs: Internal` header is visible, whether From and Sender match, whether
its subject is still the Subject line it was delivered with, that it is not a draft, and how the body was
read. **If the header is not visible**, every command will be ignored: stop and report it. (The only
fallback would be `RELAY_REQUIRE_INTERNAL_AUTH=false` *together with* an Exchange lock-down, section 13 —
an owner decision.) A `WARN` line saying Graph does not show the Subject line means the service cannot
notice an email edited in the mailbox (section 4.3): commands still work, but report it to the owner.

### 6.4 Dry-run tests (nothing reaches WhatsApp)

Each test email goes from a listed employee mailbox to `whatsapp@vship2000.com`. Within about a minute the
employee gets an email back. Use **every mail program** staff use (classic Outlook, new Outlook, Outlook
on the web, Outlook on phones), because each writes the body differently.

| # | Send | Expected email back |
|---|---|---|
| 1 | Subject `WA | 650 555 1234 | BK1`, body Name/Message/`##` as in the staff guide | "DRY RUN - WhatsApp update not sent", showing exactly the text the customer would get |
| 2 | Same with the Message written over three lines | Dry run; the text is one paragraph, and the notes say "the lines were joined with spaces" |
| 3 | Same without the `##` line | "WhatsApp update NOT sent: ..." explaining the missing `##` |
| 4 | Same with `Name:` left empty | Dry run with "Hello there," |
| 5 | Subject `WA | 12345` | NOT sent: phone number not valid |
| 6 | Subject `WA | +44 20 7946 0958` | Dry run to +44 20 7946 0958 |
| 7 | Reply (RE:) to the email of test 1 | Nothing at all (RE: is never a command) |
| 8 | A correct command from a mailbox NOT on the list | Nothing to the sender; `ALERT_TO` gets one notice (at most one per day) |
| 9 | A correct command with a PDF attached | Dry run listing what would happen to the file |
| 10 | A correct command with a link in the Message | The link appears as the real address, not a `safelinks.protection.outlook.com` one |

After the tests, the handled emails are in the folder **WhatsApp updates - handled** of the mailbox; the
emails of test 7 (the RE: reply) and test 8 (the unlisted sender) stay in the Inbox.

---

## 7. Stage 5: test numbers only

```
RELAY_DRY_RUN=false
RELAY_TEST_NUMBERS=<test phone 1>,<test phone 2>,<test number 3: a number you control that has NO WhatsApp, e.g. an office landline>
```

Check and restart (`whatsapp-webhook check-config > /dev/null && sudo systemctl restart whatsapp-webhook`).
Now real messages go out, but only to these numbers; a command to any other number is refused.

| # | Do | Expected |
|---|---|---|
| 1 | Command to test phone 1 | The phone shows the template with the name and message; the employee gets "WhatsApp update sent"; the WhatsApp Business app on the business phone shows the message in that chat |
| 2 | Command with a two-line Message | One paragraph on the phone (if Meta refuses line breaks, as reported, the service already joined them) |
| 3 | Command with an 820-character Message and a long Name | Arrives in full (820 is the limit, chosen so that the whole text stays within Meta's 1,024 characters). An 821-character Message is refused before anything is sent |
| 4 | Command to a number not on the list | NOT sent: test mode |
| 5 | From test phone 1 send any WhatsApp message to the business, then a command to it with a PDF attached | The template, then the PDF as a separate message (the 24-hour window is open) |
| 6 | Command with a PDF to test phone 2, which has not written in the last 24 h | Template only; the confirmation says the file could not be sent outside the 24-hour window (until the document template of section 9 exists) |
| 7 | Command to test number 3 (no WhatsApp) | "sent" at first, then within minutes "WhatsApp could not deliver your update" |
| 8 | Reply on test phone 1 to the update | The reply arrives by email to `MAIL_TO`, exactly as the forwarder always did |

---

## 8. Stage 6: go live

```
RELAY_TEST_NUMBERS=
```

(empty), check and restart, and give staff [whatsapp-updates-staff.md](whatsapp-updates-staff.md). Watch
the log and `/healthz` for the first days (section "WhatsApp updates from email" in
[operations.md](operations.md)).

---

## 9. Later: the booking and document templates

Both are optional and wait for the owner's wording. The code already supports them; switching them on is
a template in WhatsApp Manager plus two settings.

### 9.1 Booking template (used when the subject carries a booking number)

1. Create a **Utility** template, name `vship_booking_update`, language **English (US)**, body with the
   owner's wording. It must contain the name, the booking number and the message as variables, in any
   order, for example `Hello {{1}}, VShip has an update on booking {{2}}:` / `{{3}}` / ... .
2. When it is **Approved** and **Utility**, set on the VM, listing what fills `{{1}}`, `{{2}}`, `{{3}}` in
   that order:

   ```
   RELAY_BOOKING_TEMPLATE_NAME=vship_booking_update
   RELAY_BOOKING_TEMPLATE_PARAMS=name,booking,message
   ```

3. Restart and run `whatsapp-webhook relay-check`. Until this is set, commands with a booking number use
   `vship_update` and the booking number appears only in the employee's confirmation.

### 9.2 Document template (files outside the 24-hour window)

WhatsApp lets a business send files freely only within 24 hours of the customer's last WhatsApp message.
Outside that window a file can only travel as the header of an approved template, and Meta's document
header accepts **PDF files only** (Meta: "Templates with Header parameter types of Document only support
PDFs"). So, by the owner's decision: outside the window only PDFs are sent, one template message per PDF;
other files are listed as not sent in the employee's confirmation.

1. Create a **Utility** template in WhatsApp Manager (as in 3.4), for example name `vship_document`,
   language **English (US)**:
   - **Header**: type **Document**. WhatsApp Manager asks for a sample PDF (any harmless PDF, no customer
     data); it uploads it for review itself.
   - **Body**: the owner's wording. Meta's Utility rules require it to be about the customer's booking or
     transaction; a body that is only a variable is treated as Marketing. Example:
     `Hello {{1}}, here is the document for your VShip booking {{2}}.`
   - Samples for each body variable.
2. When it is **Approved** and **Utility**, set on the VM what fills the body's `{{1}}`, `{{2}}`, ... in
   order, from `name`, `booking` and `filename` (leave the second line empty if the body has no
   variables):

   ```
   RELAY_DOCUMENT_TEMPLATE_NAME=vship_document
   RELAY_DOCUMENT_TEMPLATE_PARAMS=name,booking
   ```

   With `booking` in the list, a PDF can only be sent this way when the subject carries a booking number;
   otherwise the confirmation says so.
3. Restart and run `whatsapp-webhook relay-check` (it checks that the template has a document header and
   the right number of variables).

Each PDF sent this way is one more Utility template message (charged). Inside the 24-hour window the
service sends PDF, Word, Excel and PowerPoint files (up to 100 MB), JPEG/PNG pictures (5 MB) and MP4
videos (16 MB) as ordinary WhatsApp messages instead, which needs no template.

Through the API instead of the screens, a document-header template needs a sample "header handle" from
Meta's Resumable Upload API (`POST /v27.0/<APP ID>/uploads?file_name=sample.pdf&file_length=<bytes>&file_type=application/pdf`,
then `POST /v27.0/upload:<session id>` with the file, which returns `{"h": "<handle>"}`), used as
`{"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["<handle>"]}}`. WhatsApp Manager
does this for you, so the screens are simpler.

---

## 10. Rollback

Fast (seconds), keeps the new code:

```
RELAY_ENABLED=false
```

then `whatsapp-webhook check-config > /dev/null && sudo systemctl restart whatsapp-webhook`. The mailbox is
no longer read; command emails stay in the Inbox. The forwarder is unaffected.
When `RELAY_ENABLED=true` is set again later, command emails that arrived in the meantime and are less
than `RELAY_MAX_AGE_HOURS` (24) old are handled, and sent unless dry run is on; older ones are refused with
a notice to their sender. Move or delete such emails first if they must not go out.

Full (back to 570fd03):

```bash
# on the administrator's PC: a clean checkout of 570fd03 (never the main checkout with uncommitted work)
git -C <repository folder> worktree add <repository folder>-570fd03 570fd03
cd <repository folder>-570fd03
PROJECT_ID=<project> DOMAIN=whatsapphook.vship2000.com ACME_EMAIL=thevshipapp@gmail.com bash deploy/gcp/deploy.sh
```

- The `RELAY_*` lines and `WA_WABA_ID` can stay in the env file: 570fd03 ignores settings it does not
  know.
- The new database tables stay and are ignored by 570fd03. Relay items still waiting in the queue would
  be marked failed by 570fd03 without an alert ("unknown queue item kind"); they never send anything.
- To return to the exact database of before (only if the queue is empty, `whatsapp-webhook stats`:
  messages received since the backup are lost from the queue otherwise):

  ```bash
  sudo systemctl stop whatsapp-webhook
  sudo rm -f /var/lib/whatsapp-webhook/queue.db-wal /var/lib/whatsapp-webhook/queue.db-shm
  sudo sh -c 'cp -a /var/lib/whatsapp-webhook/before-sendUpgrade/queue.db* /var/lib/whatsapp-webhook/'
  sudo systemctl start whatsapp-webhook
  ```
- Leave the Meta templates in place (a deleted template name cannot be reused for 30 days).
- Emails already in the folder **WhatsApp updates - handled** stay there.

---

## 11. Settings reference

All in `/etc/whatsapp-webhook/env`; after a change run `whatsapp-webhook check-config > /dev/null && sudo systemctl restart whatsapp-webhook`. Defaults are what applies when a line is
missing.

| Setting | Default | Meaning |
|---|---|---|
| `RELAY_ENABLED` | `false` | Switches the feature on. Needs `MAIL_TRANSPORT=graph`, `WA_ACCESS_TOKEN`, `WA_WABA_ID`, a sending number (`RELAY_PHONE_NUMBER_ID` or `WA_PHONE_NUMBER_ID`) and at least one allowed sender |
| `RELAY_PHONE_NUMBER_ID` | `WA_PHONE_NUMBER_ID` | Business phone number id the updates are sent from (stage 3, 5.3) |
| `RELAY_DRY_RUN` | `true` | Check commands and email the employee a preview; send nothing to WhatsApp |
| `RELAY_TEST_NUMBERS` | empty | Comma-separated numbers; when set, only these may be messaged |
| `RELAY_ALLOWED_SENDERS` | empty | Comma-separated addresses that may send updates (any letter case) |
| `RELAY_MAILBOX` | `GRAPH_SENDER` | Mailbox whose Inbox is read |
| `RELAY_REQUIRE_INTERNAL_AUTH` | `true` | Only act on emails Exchange marked as sent by a signed-in internal user. Never set `false` without an Exchange lock-down (section 13) |
| `RELAY_POLL_SECONDS` | `30` | How often the Inbox is read (at least 10) |
| `RELAY_DEFAULT_REGION` | `US` | Country assumed for 10-digit numbers |
| `RELAY_TEMPLATE_NAME` | `vship_update` | The update template |
| `RELAY_TEMPLATE_LANGUAGE` | `en_US` | Language code of all templates (must match WhatsApp Manager exactly) |
| `RELAY_TEMPLATE_PARAMS` | `name,message` | What fills `{{1}}`, `{{2}}`, ... of the update template |
| `RELAY_BOOKING_TEMPLATE_NAME` | empty | Template used when the subject has a booking number (section 9.1) |
| `RELAY_BOOKING_TEMPLATE_PARAMS` | empty | Its variables in order, from `name`, `booking`, `message` (all three required) |
| `RELAY_DOCUMENT_TEMPLATE_NAME` | empty | Template with a document header for files outside the 24-hour window (section 9.2) |
| `RELAY_DOCUMENT_TEMPLATE_PARAMS` | empty | Its body variables in order, from `name`, `booking`, `filename` (may be empty) |
| `RELAY_TEMPLATE_CHECK_SECONDS` | `600` | How long a template's Approved/Utility status is trusted before asking Meta again |
| `RELAY_MAX_MESSAGE_CHARS` | `820` | Longest Message accepted (820 keeps the whole text within Meta's 1,024-character body limit) |
| `RELAY_MAX_NAME_CHARS` | `60` | Longest Name accepted |
| `RELAY_NAME_FALLBACK` | `there` | Greeting word when Name is empty |
| `RELAY_MAX_AGE_HOURS` | `24` | Commands handled later than this after arriving are refused |
| `RELAY_FOLDER` | `WhatsApp updates - handled` | Folder handled commands are moved to (created automatically) |
| `WA_PHONE_NUMBER_ID` | (existing) | Unchanged meaning (forward only this number's messages; empty = all). Used as the sending number when `RELAY_PHONE_NUMBER_ID` is empty |
| `WA_WABA_ID` | (existing) | Becomes required: used to check the templates' status and category |
| `ALERT_TO` | `MAIL_TO` | Also receives the feature's alerts |

---

## 12. Decisions this is built on

Every point below was put to the owner on 19 Nov 2026 and answered; the code follows these answers.

| # | Question | Decision |
|---|---|---|
| 1 | Two addresses in the request (`whatsapp@VShip2000.com`, `whatsapp@vship.com`) | `whatsapp@vship2000.com`, the mailbox the forwarder already sends from |
| 2 | The uncommitted staff-replies work in the main checkout | Not part of this branch; reply behaviour stays as deployed at 570fd03. Its building blocks (Graph mailbox reader, WhatsApp send client, internal-sender checks, HTML-to-text) were copied and adapted, without any reply behaviour |
| 3 | Who is "a VShip Employee" | A fixed list of addresses (`RELAY_ALLOWED_SENDERS`). The service also requires Exchange's mark for a signed-in internal sender, From = Sender (no "on behalf of"), and never treats automatic mail as a command (security, so the list cannot be beaten by faking a From line) |
| 4 | The booking number has no place in the template | When present, a second template with the booking number (wording later, section 9.1); until it exists, the booking number is only shown to the employee. Without a booking number in the subject: the template as written |
| 5 | Who hears the outcome | The employee, for both outcomes; no copy to `MAIL_TO` |
| 6 | Late delivery failures | The employee gets an email when WhatsApp reports later that an update was not delivered |
| 7 | `RE:` / `FW:` in front of the subject | Not a command, ignored |
| 8 | Other mail in the Inbox | Left untouched |
| 9 | Line breaks in the Message (WhatsApp refuses them in a variable) | Joined with one space |
| 10 | Booking template not configured yet | Use `vship_update`; booking number only in the confirmation |
| 11 | Template filed as Marketing (not delivered to US numbers) | Checked before every send; nothing is sent unless Approved + Utility; employee and `ALERT_TO` told |
| 12 | Template name and language | `vship_update`, `en_US`; the booking template, once its wording exists, `vship_booking_update`, also `en_US` |
| 13 | Phone number format | 10 digits (or 1 + 10) = US; any other country with `+` and country code |
| 14 | Check the number in VShipCRM | No blocking check; the confirmation says whether and which CRM customer has the number |
| 15 | Empty Name | Greeting word "there" ("Hello there,") |
| 16 | Longest Message | First 1,000 characters; then, once research showed Meta's 1,024-character limit for a template body, **820** so that the whole text (fixed text 138 + Name up to 60 + Message) always fits |
| 17 | WhatsApp gives no clear answer | Never retried (no duplicates); employee told "may not have been sent" |
| 18 | Old commands | Refused when handled more than 24 hours after they arrived |
| 19 | Identical repeat commands | Always sent |
| 20 | Rollout | Dry-run first, then test numbers only, then live |
| 21 | Exchange lock-down of the mailbox | No; the service's own checks only |
| 22 | Command-shaped email from someone not on the list | Ignored, left in the Inbox, `ALERT_TO` told at most once a day |
| 23 | Attachments | Inside the customer's 24-hour window: each file as its own WhatsApp message. Outside it: through a document template (section 9.2) and, because Meta's document header takes PDF only, **only PDFs**. Otherwise text only, and the confirmation says which files were not sent |
| 24 | Pictures pasted into the email text | Never sent; only attached files. Later (review): a non-picture file that a mail program marks "inline" (iPhone Mail and Apple Mail do this with attached PDFs) counts as attached and follows decision 23; only pictures count as pasted |
| 25 | Where handled commands go | One folder, **WhatsApp updates - handled** |
| 26 | Customer replies | Exactly as deployed (to `MAIL_TO`, Reply-To = CRM emails) |
| 27 | Parse rules | The exact rules shown to the owner were accepted as shown (the staff guide repeats them) |
| 28 | Live state of the VM unknown | The guide finds it out first (stage 0); if the VM uses SMTP, the forwarder moves to Graph (section 4.2) |

Values chosen during the build that were not asked separately: the mailbox is read every 30 seconds
(setting `RELAY_POLL_SECONDS`); the 24-hour window is treated as closed 10 minutes early, so a file is
never sent into a window that closes in transit (fixed in the code). The 10-minute template re-check, the
60-character Name limit and the 24-hour age limit were part of the options the owner chose.

Built in on purpose, beyond the decisions (safety, not behaviour): a mistake in a `RELAY_*` setting never
stops the service or the forwarder, it only leaves the feature off (section 5.3); the sending number has
its own setting so that switching the feature on never changes which messages are forwarded (5.3).

---

## 13. Known limits

### 13.1 What only the live tests can prove

| Point | Why it is open | Proven by |
|---|---|---|
| Exchange's internal-sender mark is visible to the service | Microsoft documents the header on delivered mail but not, in so many words, in Graph's header list | `relay-check --sample` (6.3) |
| The Subject line a command was delivered with is visible to the service | Needed to notice an email edited in the mailbox after it arrived (section 4.3); not documented for Graph's header list either | `relay-check --sample` (6.3) |
| Line breaks in `{{2}}` are refused by WhatsApp | Reported consistently by developers through October 2026 (error 132018, "Param text cannot have new-line/tab characters or more than 4 consecutive spaces") but not on a current Meta reference page. The service joins lines in any case, so this only matters if the owner later wants paragraphs | not needed for go-live |
| 820 characters arrive in full | Meta states 1,024 characters for a template body; whether it counts the filled-in text is not documented (a 2022 note said 32,768 for body-only templates). 820 keeps the filled-in text within 1,024 in every case | stage 5, test 3 |
| How each Outlook program's text is read | Microsoft does not document how Exchange turns HTML into text; the service converts the HTML itself | dry-run tests with every mail program (6.4) |
| Template sends show in the WhatsApp Business app on the business phone (Coexistence) | Meta says 1:1 messages are mirrored, without naming templates | stage 5, test 1 |

### 13.2 Limits to know

- **Marketing re-filing.** Meta can re-file an approved Utility template as Marketing after seeing what is
  sent through it. Then nothing reaches US numbers. The service then refuses to send (and tells the
  employee and `ALERT_TO`) instead of sending into the void. Keep updates about bookings; never promotions.
- **Messaging limit.** A new portfolio can reach 250 different customers per rolling 24 hours with
  business-initiated messages (2,000 after business verification).
- **One customer, many employees.** Nothing stops two employees sending the same customer an update.
- **Replies go to the team, not to the sender.** A customer's WhatsApp answer is forwarded to `MAIL_TO`
  like every other WhatsApp message (deployed behaviour, kept on purpose).
- **Customers without a phone number in WhatsApp** (username only) cannot be reached this way: the
  command needs a phone number.
- **Delay.** The Inbox is read every 30 seconds; with Meta's answer the update usually goes out within a
  minute.
- **Junk and quarantine.** A command email that Microsoft's filters hold never reaches the Inbox, so no
  answer comes back. Internal mail is rarely held; check https://security.microsoft.com/quarantine if an
  employee gets no answer. An email moved into the Inbox later (rescued from Junk Email) is not picked up
  if it arrived more than 15 minutes earlier: the employee has to send it again.
- **Replaying a failed update** (`whatsapp-webhook replay-failed`) never sends anything that went out, or
  may have gone out, before. An update given up after its email was read and its sender checked only
  finishes the filing on replay and sends nothing more (its sender was already told what did and did not
  go out); one given up before its email could be read is handled as new, and refused if it is more than
  24 hours old. Details: [operations.md](operations.md), "Delivery failures".
- **Automatic replies** from employees who are away will arrive in the mailbox's Inbox in answer to
  confirmations. They are not commands and are left there.
- **Sent Items.** Every confirmation is also kept in the mailbox's Sent Items, like the forwarded emails.
- **No stop list.** WhatsApp's policy requires honouring stop requests; the service does not keep a list,
  so staff must.
- **Template edits.** An approved template can be edited at most once per 24 hours and 10 times per 30
  days, its category never; each edit is reviewed again (up to 24 hours).
- **Meta's account model change** (2026-2028): a send must name its Messaging account only when the token
  reaches several Messaging accounts on the same number. With VShip's single account nothing is needed;
  re-check before mid-2027 (Meta's "account model evolution" pages).

### 13.3 If the internal-sender mark is not visible, or you want Exchange to refuse outside mail too

The owner decided against an Exchange lock-down (decision 21). Should it become necessary, this makes
Exchange refuse every email to the mailbox that does not come from a signed-in member of the organisation
(outsiders get "550 5.7.134"):

```powershell
Set-Mailbox -Identity whatsapp@vship2000.com -RequireSenderAuthenticationEnabled $true
# undo:
Set-Mailbox -Identity whatsapp@vship2000.com -RequireSenderAuthenticationEnabled $false
```

Microsoft does not document whether this also stops bounce messages for the forwarded emails, which
arrive in the same Inbox: external servers' bounces would be refused (they are anonymous). Test first:
`whatsapp-webhook test-email --to <non-existent>@vship2000.com` and
`whatsapp-webhook test-email --to <non-existent address at another domain>`, then check that both bounces
still arrive. This proves only that bounces written by Exchange itself still arrive (a bad external
address is usually refused while Exchange is still talking to the other server, and Exchange then writes
the bounce); a bounce that an external server sends later would still be refused.

---

## 14. Sources

Meta (all under `https://developers.facebook.com/documentation/business-messaging/whatsapp/`):

- `templates/template-library` (document headers take PDF only), `templates/overview`,
  `templates/components`, `templates/template-review`,
  `templates/template-categorization` (Utility rules; automatic Marketing approval since 9 Apr 2025),
  `templates/template-pausing`, `templates/template-management` (132018 for parameter problems since v23)
- `templates/marketing-templates/per-user-limits` and `marketing-messages/get-started` (Marketing
  templates not delivered to US numbers since 1 Apr 2025, error 131049)
- `messages/send-messages`, `reference/whatsapp-business-phone-number/message-api`,
  `webhooks/reference/messages/status`, `support/error-codes`
- `pricing` (per-message pricing; Utility charged inside the window from 1 Oct 2026),
  `messaging-limits`, `throughput`, `permissions`, `solution-providers/app-review` (Standard access is
  enough for your own app), `account-model-evolution/messaging` (messaging_account_id only with several
  Messaging accounts), `display-names`, `embedded-signup/onboarding-business-app-users` (Coexistence)
- WhatsApp Business Messaging Policy, https://whatsappbusiness.com/policy/ (updated 23 Sep 2026)
- Reported but not on a current Meta reference page: the 132018 detail text "Param text cannot have
  new-line/tab characters or more than 4 consecutive spaces" (developer reports through March 2026)

Microsoft (all under `https://learn.microsoft.com/`):

- `graph/api/mailfolder-list-messages`, `graph/api/message-get`, `graph/api/message-move`,
  `graph/api/mailfolder-post-childfolders`, `graph/outlook-immutable-id`, `graph/throttling-limits`
- `exchange/permissions-exo/application-rbac` (Application Mail Full Access = Mail.ReadWrite + Mail.Send;
  30 min to 2 h to take effect)
- `exchange/header-firewall-exchange-2013-help` and the Exchange Team blog "Demystifying hybrid mail flow:
  when is a message internal?" (`X-MS-Exchange-Organization-AuthAs`)
- `defender-office-365/safe-links-about` (links in internal mail can be rewritten)
- `exchange/recipients-in-exchange-online/manage-user-mailboxes/configure-message-delivery-restrictions`
