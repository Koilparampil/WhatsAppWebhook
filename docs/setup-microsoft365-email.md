# Microsoft 365: sending path, SPF, DKIM, DMARC and Network Solutions DNS

This guide covers how the forwarder delivers email into your Microsoft 365 tenant and which DNS
records belong at Network Solutions. It was written against Microsoft Learn, Exchange Team blog and
Network Solutions help pages as of November 2026 (sources at the end). Values used in this guide:

| Value | Meaning |
|---|---|
| `vship2000.com` | the Microsoft 365 mail domain (From address domain); DNS hosted at Network Solutions (`ns89/ns90.worldnic.com`) |
| `vshipnyc.onmicrosoft.com` | the tenant's initial domain (visible in the existing DKIM CNAME targets) |
| `whatsapp@vship2000.com` | proposed address of the shared mailbox the forwarder sends from (confirm or rename) |
| `whatsapphook.vship2000.com` | the hostname Caddy serves for the WhatsApp webhook |
| `203.0.113.10` | placeholder for the VM's reserved static IPv4 address (printed by `provision.sh`) |

**State of the public DNS on 9 Nov 2026** (checked with Google Public DNS):

| Record | Found | Action |
|---|---|---|
| `vship2000.com` TXT (SPF) | `v=spf1 include:spf.protection.outlook.com -all` | Correct for Microsoft 365. **No change.** |
| `vship2000.com` MX | `vship2000-com.mail.protection.outlook.com` | Mail is on Exchange Online. No change. |
| `selector1._domainkey` / `selector2._domainkey` CNAME | `selector1-vship2000-com._domainkey.vshipnyc.onmicrosoft.com` (and selector2) | DKIM records exist. Only confirm the toggle is **Enabled** in the Defender portal (3d). |
| `_dmarc.vship2000.com` TXT | `v=DMARC1; p=none;` | Present, monitoring mode, no report address. Optional: add `rua=` (3e). |
| `whatsapphook.vship2000.com` A | does not exist | **Create it** after provisioning the VM (3b). |

## 1. Which sending method to use (decision)

Four ways exist for an application to send email through Microsoft 365. Measured against this
project (one VM on Google Compute Engine, internal recipients, attachments up to tens of MB,
per-message Reply-To):

| Method | Verdict | Why |
|---|---|---|
| **Microsoft Graph `sendMail`** with an Entra app (client credentials) | **Recommended** | HTTPS only, per-message `replyTo`, attachments up to 150 MB per file via upload sessions, can be scoped to one mailbox, no DNS changes. |
| SMTP AUTH on `smtp.office365.com:587` with **OAuth** (`SMTP.SendAsApp`) | Fallback | Works, but Microsoft recommends disabling SMTP AUTH where possible and the service principal needs Full Access on the mailbox. |
| SMTP AUTH with **Basic** auth (username/password) | Do not use | Microsoft disables it **by default at the end of December 2026** for existing tenants; final removal to be announced in 2027. |
| **Direct Send** (port 25 to `vship2000-com.mail.protection.outlook.com`) | Not viable | Google Compute Engine **blocks outbound TCP 25** by default; mail is treated as anonymous internet mail (spam-filtered, no DKIM); Microsoft is moving it to off-by-default (`RejectDirectSend`). |
| High Volume Email / Azure Communication Services | Not suitable | Both cap messages at 10 MB; ACS Email is retiring (unavailable after 30 Sep 2028). |

**Consequence for SPF and DKIM:** with Graph (or OAuth SMTP AUTH) the VM never sends mail itself.
Messages are submitted into Exchange Online as the mailbox and are classified as *internal*, so
no SPF `ip4:` entry for the VM is needed and nothing changes for deliverability. The records in
section 3 are standard Microsoft 365 hygiene that every tenant should have anyway. **Do not add the
VM's IP to SPF** unless you deliberately move to Direct Send (which this platform cannot do).

The service supports both transports: `MAIL_TRANSPORT=graph` (recommended) and `MAIL_TRANSPORT=smtp`
with `SMTP_AUTH=oauth`.

## 2. Microsoft Graph setup (recommended path)

Roles needed: a Global Administrator, or an Exchange Administrator who is also in the Exchange
Online **Organization Management** role group, plus Cloud Application Administrator for the app
registration. PowerShell steps run on the administrator's PC in Exchange Online PowerShell, not on
the VM.

### 2a. Create the sending mailbox

1. Create `whatsapp@vship2000.com`. A **shared mailbox** needs no licence (up to 50 GB) and keeps
   sign-in blocked, which is fine because the app authenticates as itself. Microsoft 365 admin
   center: **Teams & groups > Shared mailboxes > Add a shared mailbox**. A licensed user mailbox also
   works.
2. The display name of the mailbox is what recipients see as the sender name when the Graph *draft*
   path is used for large emails; the forwarder sets `"<WhatsApp sender> via WhatsApp"` where the
   API allows it.

### 2b. Register the application in Microsoft Entra

3. https://entra.microsoft.com > **Entra ID > App registrations > New registration** (older portal
   wording: Identity > Applications > App registrations).
   Name `WhatsApp Webhook Mailer`, **Accounts in this organizational directory only**, no redirect
   URI, **Register**.
4. On **Overview** copy **Application (client) ID** → `OAUTH_CLIENT_ID` and **Directory (tenant) ID**
   → `OAUTH_TENANT_ID`.
5. **Certificates & secrets > Client secrets > New client secret**, description `whatsapp-webhook-vm`,
   expiry 12 months or less, **Add**, copy the **Value** immediately → `OAUTH_CLIENT_SECRET`. The value
   is never shown again. Put the expiry date in the team calendar. (Microsoft recommends a
   certificate for production; a secret is acceptable for this internal tool if rotation is tracked.)
6. **API permissions**: choose ONE of the two authorization models below.

   **Model A, least privilege (recommended):** add **no** Graph permissions here. Rights are granted
   to one mailbox only with Exchange "RBAC for Applications" (2c). Entra grants and Exchange RBAC
   grants are a *union*, so a tenant-wide Entra grant would defeat the scope.

   **Model B, simplest:** **Add a permission > Microsoft Graph > Application permissions**, tick
   **Mail.Send** and **Mail.ReadWrite**, **Grant admin consent for <tenant>**. The app can then act on
   every mailbox in the tenant; skip 2c.
7. **Entra ID > Enterprise applications** (older wording: Identity > Applications > Enterprise
   applications), open `WhatsApp Webhook Mailer`, copy its
   **Object ID** (the service principal id; it differs from the App registration Object ID).

### 2c. Scope the app to the one mailbox (Model A, Exchange Online PowerShell)

```powershell
Install-Module -Name ExchangeOnlineManagement
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -UserPrincipalName admin@vship2000.com

# AppId = Application (client) ID from step 4; ObjectId = Enterprise application Object ID from step 7
New-ServicePrincipal -AppId <Application (client) ID> -ObjectId <Enterprise app Object ID> -DisplayName "WhatsApp Webhook Mailer"

New-ManagementScope -Name "WhatsApp relay sender" -RecipientRestrictionFilter "UserPrincipalName -eq 'whatsapp@vship2000.com'"

# "Application Mail Full Access" = Mail.ReadWrite + Mail.Send; needed because attachments of 3 MB or
# more are sent through a draft message and an upload session. "Application Mail.Send" alone is
# enough only if media will never exceed 3 MB.
New-ManagementRoleAssignment -App <Enterprise app Object ID> -Role "Application Mail Full Access" -CustomResourceScope "WhatsApp relay sender"

# Verify (bypasses the cache): InScope must be True for the relay mailbox and False for any other mailbox
Test-ServicePrincipalAuthorization -Identity <Enterprise app Object ID> -Resource whatsapp@vship2000.com | Format-Table
```

Live API calls can take 30 minutes to 2 hours to reflect the assignment.

### 2d. Message size limits (decides the split threshold)

```powershell
Get-Mailbox whatsapp@vship2000.com | Format-List MaxSendSize,MaxReceiveSize
Get-MailboxPlan | Format-List Name,IsDefault,MaxSendSize,MaxReceiveSize
```

Defaults are **35 MB send / 36 MB receive**, applied to the encoded message (base64 adds about 33%
to attachments), configurable between 1 MB and 150 MB. The forwarder keeps every email under
`MAIL_MAX_MESSAGE_BYTES` (default 32,000,000 bytes) and splits larger media across numbered emails.
The decision for this project is to **keep the 32 MB threshold**. For reference, this is how the
tenant limits would be raised later so that fewer, larger emails are needed. Limits apply to the
**sending mailbox and every receiving mailbox**, and the value must include the base64 overhead
(a 70 MB file needs about 95 MB of message size).

Worked example, raising everything to 100 MB:

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@vship2000.com

# 1. the sender
Set-Mailbox -Identity whatsapp@vship2000.com -MaxSendSize 100MB -MaxReceiveSize 100MB

# 2. every existing mailbox that will receive the forwarded emails (or all mailboxes)
Set-Mailbox -Identity ops@vship2000.com -MaxReceiveSize 100MB
# Get-Mailbox -ResultSize Unlimited | Set-Mailbox -MaxReceiveSize 100MB

# 3. mailboxes created in the future
Get-MailboxPlan | Set-MailboxPlan -MaxSendSize 100MB -MaxReceiveSize 100MB

# 4. confirm
Get-Mailbox whatsapp@vship2000.com, ops@vship2000.com | Format-List Name,MaxSendSize,MaxReceiveSize
```

Then, in `/etc/whatsapp-webhook/env`, set `MAIL_MAX_MESSAGE_BYTES=95000000` (about 10% below the new
limit) and restart the service. The service's memory ceiling in the systemd unit (`MemoryMax=600M`)
is sized for parts up to roughly 90 MB; raise it together with the limit. The hard maximum is 150 MB,
Outlook mobile still caps at 33 MB per message, and Outlook on the web at 112 MB, so very large
emails can only be opened in desktop Outlook.

### 2e. Sent Items

Graph saves a copy of each sent message in the relay mailbox's **Sent Items**. With attachments this
grows by roughly the volume of forwarded media. Either apply a retention tag that deletes Sent Items
after 30 days (Microsoft Purview > Data lifecycle management > Exchange retention tags), or leave it
and watch the 50 GB shared-mailbox quota.

### 2f. Service configuration

```
MAIL_TRANSPORT=graph
OAUTH_TENANT_ID=<Directory (tenant) ID>
OAUTH_CLIENT_ID=<Application (client) ID>
OAUTH_CLIENT_SECRET=<secret value>
GRAPH_SENDER=whatsapp@vship2000.com
MAIL_FROM=whatsapp@vship2000.com
MAIL_TO=<internal recipient(s)>
```

Only outbound HTTPS to `login.microsoftonline.com`, `graph.microsoft.com` and `outlook.office.com`
(upload sessions) is needed. Test with `whatsapp-webhook test-email`. Throttling to respect: 30
messages per minute and 10,000 recipients per day per mailbox; 150 MB uploaded per 5 minutes per
app and mailbox. The worker sends one email at a time and backs off on HTTP 429.

Two Graph-specific behaviours to know: emails whose attachment is 3 MB or larger go through a draft
message plus an upload session, and Exchange then assigns its own `Message-ID`, so the forwarder's
deterministic ids and the `In-Reply-To` threading of quoted replies apply only to smaller emails on
this transport (they always apply on SMTP). Graph also keeps a copy of every sent email in the relay
mailbox's Sent Items (section 2e).

Three more operational notes:

- Microsoft documents Graph application-permission sending from user mailboxes; sending from a
  sign-in-blocked **shared mailbox** works in practice but is not spelled out in Microsoft's pages.
  Run `whatsapp-webhook test-email` right after setup; if Graph answers 403 or 404 for the shared
  mailbox, assign it a licence or use a licensed user mailbox instead.
- `sendMail` returns 202 Accepted without guaranteeing delivery. Bounces (for example a message over
  a recipient's size limit) arrive as NDRs in the relay mailbox's Inbox, so someone should glance at
  `whatsapp@vship2000.com` occasionally, or add it to a shared Outlook folder.
- Internal mail bypasses most filtering, but the anti-spam policy still quarantines intra-organisation
  messages classified as high-confidence phishing. If a test email never arrives, check
  https://security.microsoft.com/quarantine before suspecting the service.

### 2g. Fallback: SMTP AUTH with OAuth (only if Graph is blocked by policy)

1. Entra app as above, but under **API permissions > APIs my organization uses > Office 365 Exchange
   Online > Application permissions** add **SMTP.SendAsApp** and grant admin consent.
2. Exchange Online PowerShell:
   `New-ServicePrincipal -AppId <client id> -ObjectId <enterprise app object id>` then
   `Add-MailboxPermission -Identity whatsapp@vship2000.com -User <service principal id> -AccessRights FullAccess`.
3. Enable SMTP AUTH for that mailbox only: Microsoft 365 admin center > Users > Active users >
   mailbox > Mail > Manage email apps > tick **Authenticated SMTP**, or
   `Set-CASMailbox -Identity whatsapp@vship2000.com -SmtpClientAuthenticationDisabled $false`.
   (SMTP AUTH is disabled tenant-wide for tenants created after January 2020 and must be enabled per mailbox.)
4. Service configuration: `MAIL_TRANSPORT=smtp`, `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`,
   `SMTP_SECURITY=starttls`, `SMTP_AUTH=oauth`, `SMTP_USERNAME=whatsapp@vship2000.com`, plus the
   `OAUTH_*` values. The service requests tokens for `https://outlook.office365.com/.default` and
   authenticates with SASL XOAUTH2.

## 3. DNS at Network Solutions

### 3a. Opening the DNS editor

Network Solutions has more than one UI generation in 2026. Sign in at
https://www.networksolutions.com/my-account/login (older link: https://www.networksolutions.com/manage-it)
and use whichever of these you see:

- **Domains** (left menu) > select `vship2000.com` > **Manage** > **DNS** tab > **+Add Record**
  (edit an existing record through its vertical ellipsis > **Edit**), or
- **Domain Names** > tick the domain > **Actions** (three dots) > **Manage** > scroll to **Advanced
  Tools** > **MANAGE** next to **Advanced DNS Records** (click **Continue** if asked) > **+ADD RECORD**, or
- the domain's **SETTINGS** button > **Advanced Tools** > **Manage** next to Advanced DNS Records > **+Add Record**.

Field conventions: **Refers to** `@` means the apex (it displays as `@ (None)` after saving);
**Other Host** plus a **Host Name** means a subdomain; enter only the label (`whatsapphook`, `_dmarc`,
`selector1._domainkey`), never the full domain. Targets must **not** end with a period. Default TTL
is 7200 seconds; 3600 is fine. Changes propagate in about 15 minutes to 2 hours.

If the domain shows no DNS editor, its nameservers are hosted elsewhere: check with
`dig NS vship2000.com +short` and edit the records at that host instead.

### 3b. A record for the webhook host (required for Caddy)

Type **A**, Refers to **Other Host**, Host Name `whatsapphook`, Points to the static IP printed by
`provision.sh`, TTL 3600. Create this **before** running the deploy script; Caddy only requests a
certificate once the name resolves to the VM, and repeated failed attempts count against Let's
Encrypt rate limits. Check with `dig +short whatsapphook.vship2000.com` (or `nslookup`).

### 3c. SPF (one record only)

**vship2000.com already publishes `v=spf1 include:spf.protection.outlook.com -all`, which is exactly
what Microsoft 365 needs. Nothing to do.** The general rules, should the record ever be edited:

First inspect what exists: `dig TXT vship2000.com +short`. Exactly one string may start with `v=spf1`.

- **None exists:** Type **TXT**, Refers to `@`, TXT Value `v=spf1 include:spf.protection.outlook.com -all`, TTL 3600.
- **One exists:** **edit** it (never add a second one, two SPF records make SPF fail for the whole
  domain). Keep every mechanism for services the organisation really uses, make sure
  `include:spf.protection.outlook.com` is present, end with `-all`. Paste the value, do not type it:
  no trailing period, colon after `include`, no space after the colon.
- Stay under 10 DNS lookups: each `include:`, `a`, `mx`, `exists`, `redirect` costs one (nested
  includes add more); `ip4:`/`ip6:` do not count.

The VM's IP does **not** go into this record (section 1).

### 3d. DKIM (two CNAME records, values copied from Microsoft)

**Both DKIM CNAME records already exist for vship2000.com** and point at
`selector1-vship2000-com._domainkey.vshipnyc.onmicrosoft.com` and the selector2 equivalent. The
only check left is step 1 below: the Defender portal must show the domain's toggle **Enabled** with
Status **Valid**. If it shows **CnameMissing** or **Disabled**, follow step 5 to switch signing on; do
not re-create the records.

Intra-tenant mail is not DKIM-signed by design, so this does not affect the forwarder's emails. It
is still recommended hygiene for every Microsoft 365 domain. Note: Microsoft now issues two CNAME
target formats (domains added since May 2025 use `...<prefix>.<x>-v1.dkim.mail.microsoft`, older
ones `...<prefix>.onmicrosoft.com`). **Always copy the values from the portal; never build them by hand.**

1. https://security.microsoft.com > **Email & collaboration > Policies & rules > Threat policies >
   Email authentication settings > DKIM** tab (direct link
   https://security.microsoft.com/authentication?viewid=DKIM). If the domain already shows Status
   **Valid** with the toggle **Enabled**, DKIM is done; skip this section.
2. Slide the domain's toggle to **Enabled**. A **Client error** dialog appears; click **OK**. Status
   becomes **CnameMissing**. Click the row to open the flyout and **Copy** the two values under
   **Publish CNAMEs**. (PowerShell alternative:
   `Get-DkimSigningConfig -Identity vship2000.com | Format-List Selector1CNAME,Selector2CNAME`.)
3. At Network Solutions add two records: Type **CNAME**, Refers to **Other Host**, Host Name
   `selector1._domainkey`, Alias to = the exact Selector1CNAME value, TTL 3600; repeat for
   `selector2._domainkey` with the Selector2CNAME value. Both are mandatory. If the editor refuses a
   host name with an underscore, contact Network Solutions support (their help defers Microsoft 365
   DKIM to Microsoft's instructions, so it is supported).
4. Wait until both resolve publicly:
   `dig CNAME selector1._domainkey.vship2000.com +short` and `... selector2 ...` (minutes to 2 hours;
   worst case 48 hours).
5. Back in the flyout turn on **Sign messages for this domain with DKIM signatures**. Success: toggle
   **Enabled**, Status **Signing DKIM signatures for this domain**. PowerShell alternative:
   `Set-DkimSigningConfig -Identity vship2000.com -Enabled $true`.

### 3e. DMARC (monitoring mode first)

**vship2000.com already has `_dmarc` = `v=DMARC1; p=none;`**, so DMARC is in monitoring mode but no
one receives the aggregate reports. Optional improvement:

1. Create a shared mailbox for reports, e.g. `dmarc-reports@vship2000.com`.
2. At Network Solutions **edit** the existing `_dmarc` TXT record (do not add a second one) to
   `v=DMARC1; p=none; rua=mailto:dmarc-reports@vship2000.com`, TTL 3600.
3. Review the aggregate reports weekly. After several clean weeks move to
   `v=DMARC1; p=quarantine; pct=25; rua=mailto:dmarc-reports@vship2000.com`, raise `pct` to 100, then
   `p=reject`. Tightening early can junk legitimate mail from other services that send as the domain.

### 3f. Verification

```bash
dig TXT vship2000.com +short                          # one v=spf1 ... include:spf.protection.outlook.com -all
dig TXT _dmarc.vship2000.com +short                   # one v=DMARC1 record
dig CNAME selector1._domainkey.vship2000.com +short   # Microsoft target
dig CNAME selector2._domainkey.vship2000.com +short
dig A whatsapphook.vship2000.com +short                    # the VM's static IP
dig @8.8.8.8 TXT vship2000.com +short                 # bypass local caches
```

Microsoft side: the Defender DKIM tab shows **Enabled / Valid**; https://testconnectivity.microsoft.com
checks SPF, DKIM and DMARC. To see DKIM in action send a test from the relay mailbox to an external
outlook.com or gmail.com address and paste the headers into https://mha.azurewebsites.net/ (expect
`spf=pass`, `dkim=pass header.d=vship2000.com`, `dmarc=pass`). Internal test messages show
`dkim=none` by design and the header `X-MS-Exchange-Organization-AuthAs: Internal`.

## 4. Sources

- Microsoft Learn: [Set up SPF](https://learn.microsoft.com/en-us/defender-office-365/email-authentication-spf-configure),
  [Set up DKIM](https://learn.microsoft.com/en-us/defender-office-365/email-authentication-dkim-configure),
  [Set up DMARC](https://learn.microsoft.com/en-us/defender-office-365/email-authentication-dmarc-configure),
  [Connect DNS records at Network Solutions](https://learn.microsoft.com/en-us/microsoft-365/admin/dns/create-dns-records-at-network-solutions),
  [Options for sending email from devices and applications](https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/how-to-set-up-a-multifunction-device-or-application-to-send-email-using-microsoft-365-or-office-365),
  [Deprecation of Basic authentication in Exchange Online](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online),
  [Authenticate SMTP with OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
  [RBAC for Applications in Exchange Online](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac),
  [user: sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail),
  [Attach large files to Outlook messages](https://learn.microsoft.com/en-us/graph/outlook-large-attachments),
  [Exchange Online limits](https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits),
  [Graph throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits).
- Exchange Team blog: [Updated SMTP AUTH Basic Authentication deprecation timeline (Jan 2026)](https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835),
  [Introducing more control over Direct Send](https://techcommunity.microsoft.com/blog/exchange/introducing-more-control-over-direct-send-in-exchange-online/4408790).
- Google Cloud: [Blocked traffic: outbound port 25](https://docs.cloud.google.com/vpc/docs/firewalls#blockedtraffic).
- Network Solutions help: [Manage DNS and Advanced DNS records](https://www.networksolutions.com/help/article/manage-dns-adns-records),
  [SPF records](https://www.networksolutions.com/help/article/spf-records),
  [DKIM settings](https://www.networksolutions.com/help/article/adjust-dkim-settings).
