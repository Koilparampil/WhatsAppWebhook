# Meta / WhatsApp: System User token, webhook and go-live

Written against Meta's developer documentation and Business Help Center as of November 2026
(sources at the end). Button labels differ slightly between the classic Business Settings page and
the newer Meta Business Suite settings; the equivalents are given where they differ.

Since the account-model change Meta rolled out in September and October 2026, the former
"WhatsApp Business Account" appears in Business Suite as two things: a **WhatsApp account** (the
phone number and identity) and a **Messaging account** (templates, billing, webhook subscriptions).
Existing WABA ids are now Messaging account ids; nothing changes in the API calls this service makes.

## 1. Prerequisites

- A Meta business portfolio where you have **Full control**, a Meta developer account, and the
  existing Meta app created with the **Connect with customers through WhatsApp** use case.
- In the App Dashboard (https://developers.facebook.com/apps) > your app > **WhatsApp** (or
  **Use cases > Customize**) > **API Setup**, confirm the app is connected to your Messaging account.
  Note the **Messaging account ID** (WABA id) and the **Phone number ID** shown there.

## 2. Create the System User and generate a permanent token

1. Open Meta Business Suite settings: https://business.facebook.com/latest/settings/system_users
   (Settings > **Users** > **System users**; classic page: https://business.facebook.com/settings > System Users).
2. **Add new system user** (classic: **+Add**). Name it `whatsapp-email-forwarder`. Role: **Employee**
   (least privilege; an Admin system user automatically gets every current and future account).
   **Create system user**.
3. Assign the app: select the system user > **Assign Assets** > **Apps** > select your Meta app >
   enable **Manage app** (Full control) > **Assign assets**.
4. Assign the WhatsApp assets. From the system user: **Assign Assets** > **WhatsApp accounts** >
   select the account > enable **Manage WhatsApp Business accounts** (Full control) > Assign.
   Then confirm from the account side for **both** account types, because the panel does not cascade:
   Business Suite > portfolio **Settings** (gear) > **Accounts > WhatsApp accounts** > your account >
   **People** > **+Add people** > the system user > **Full** access; repeat under
   **Accounts > Messaging accounts**. Missing either one makes Graph calls fail with error code 200.
5. Generate the token: select the system user > **Generate new token** (classic: **Generate token**)
   > choose your Meta app > expiration **Never** > tick exactly
   **whatsapp_business_messaging** and **whatsapp_business_management** > **Generate token** > copy it.
   The token is shown once. If only an expiring option is offered, your portfolio enforces
   expiring tokens; see section 6.
6. Verify the token at https://developers.facebook.com/tools/debug/accesstoken/ or, once the
   service is configured, with `whatsapp-webhook meta-check`. Expect type `SYSTEM_USER`,
   `is_valid: true`, expires `Never`, and both `whatsapp_` scopes.

### Employee or Admin system user

| | Employee system user (recommended) | Admin system user |
|---|---|---|
| Access | Only the assets you assign: this app, this WhatsApp account, this Messaging account. | Automatically has full access to every WhatsApp and Messaging account the portfolio owns now or later, and can assign permissions to others. |
| Blast radius if the token leaks | Limited to the assigned assets. | The whole portfolio: numbers, templates, billing, other apps. Meta's own guidance is to reserve admin system users for administrative actions and to safeguard their tokens carefully. |
| Maintenance | A new WhatsApp or Messaging account must be assigned explicitly (two tabs since the 2026 account split). Forgetting one shows up as Graph error 200. | Nothing to assign when accounts are added. |
| Fit for this service | Exactly one app and one number; the token sits on a VM for years. | Convenient, but more privilege than the forwarder needs. |

Use Employee unless you plan to add numbers frequently and accept the broader token.

### How to tell whether the portfolio enforces expiring tokens

1. Open the system user and click **Generate new token**. The dialog has a **token expiration**
   selector. If it offers **Never** next to **60 days**, permanent tokens are allowed. If only
   **60 days** is offered, Meta requires expiring tokens for this portfolio; there is no setting to
   change that, and generating a non-expiring token through the API returns an error.
2. After generating, paste the token into https://developers.facebook.com/tools/debug/accesstoken/
   or run `whatsapp-webhook meta-check` on the VM: **Expires: Never** (API `expires_at: 0`) confirms a
   permanent token; a date about 60 days out means it is an expiring one.

If expiring tokens are enforced, tell us: the service then needs a scheduled refresh job (section 6)
rather than a one-time token.

## 3. Values the service needs

| Value | Where | Env var |
|---|---|---|
| App Secret | App Dashboard > **App settings > Basic** > App secret (**Show**) | `WA_APP_SECRET` |
| System User token | step 5 above | `WA_ACCESS_TOKEN` |
| Verify token | any long random string you invent, e.g. `openssl rand -hex 32` | `WA_VERIFY_TOKEN` |
| Messaging account (WABA) id | API Setup panel, or Business Settings > Accounts | `WA_WABA_ID` |
| Phone number id | API Setup panel, or `whatsapp-webhook meta-check` | `WA_PHONE_NUMBER_ID` (optional filter) |
| Graph API version | current: `v27.0` (released 4 Nov 2026; each version is supported for at least two years) | `WA_GRAPH_API_VERSION` |

Store them only in `/etc/whatsapp-webhook/env` on the VM (owner root, group whwebhook, mode 640:
readable by root and the service user only). Never commit them.

## 4. Configure the webhook

1. Deploy the service first (docs/setup-gcp.md) so `https://<host>/webhook` answers. Caddy's
   automatic certificate satisfies Meta's requirement for a valid public TLS certificate.
2. App Dashboard > **WhatsApp > Configuration** (use-case apps: **Use cases > Customize >
   Configuration**) > Webhooks: **Callback URL** `https://<host>/webhook`, **Verify token** = your
   `WA_VERIFY_TOKEN` > **Verify and save**. Meta sends the verification GET immediately; if it fails,
   check `journalctl -u caddy` and `journalctl -u whatsapp-webhook` on the VM.
3. In the fields list, **Subscribe** to **messages**. Leave other fields unsubscribed (the service
   ignores them). Click **Test** on the messages row; the service should log one queued message
   (the test payload uses a sample sender) and then email it. Leave `WA_PHONE_NUMBER_ID` empty for
   this test: the sample may carry a placeholder phone number id, and with the filter set the
   service skips it (the log then says "skipped 1 message(s) addressed to other business numbers").
4. Make sure the app is subscribed on the production Messaging account and that no callback override
   exists: on the VM run `whatsapp-webhook meta-check`. If it reports no subscribed app, run
   `whatsapp-webhook meta-subscribe` (equivalent to `POST /<WABA_ID>/subscribed_apps`).
5. Put the app in its production state. Meta states that "some webhooks will not be sent if your app
   is in Dev mode". If the dashboard toolbar shows an **App Mode** toggle, switch it to **Live**; if the
   left menu shows **Publish**, complete the listed requirements (app icon 512 to 1024 px, Privacy
   Policy URL, data deletion URL or instructions, display name, contact email, terms of service URL,
   category, app purpose) and click **Publish**. Business verification is **not** required to receive
   messages on your own number; it only raises outbound limits.

## 5. Production phone number

The business number already exists. Confirm it shows **Connected** in WhatsApp Manager > Account
tools > Phone numbers and in `whatsapp-webhook meta-check` (status `CONNECTED`). Then send a text, an
image and a PDF from a personal phone to the business number and confirm one email per message
arrives (the PDF path exercises the media download and, for large files, the splitting logic).

### Limits and optional hardening

- At Standard access an app can have **one Employee and one Admin system user**. If the portfolio's
  Employee system user is already used by another integration, reuse it (generate a second token for
  it) rather than expecting to create another.
- **Require App Secret** (App Dashboard > App settings > Advanced > Security) can be switched on:
  the service sends `appsecret_proof` with every Graph API call it makes. Do this only after the
  service is deployed with the correct `WA_APP_SECRET`, otherwise media downloads fail.
- Meta has announced the next phases of the account model: a new Graph API version with separate
  WhatsApp-account and Messaging-account permissions in the first half of 2027, and WhatsApp-account
  ids replacing phone number ids in API paths in the first half of 2028. Put a review of
  `WA_GRAPH_API_VERSION` and the token permissions in the calendar before each.

## 6. Token hygiene

- Inbound media ids expire 7 days after the webhook and download URLs 5 minutes after they are
  issued; the service downloads at processing time, so a dead-lettered message older than 7 days
  cannot be replayed with its attachment.
- **Rotation**: generate a new token for the same system user, update `WA_ACCESS_TOKEN`, restart the
  service, then revoke the old token:
  `GET https://graph.facebook.com/v27.0/oauth/revoke?client_id=<APP_ID>&client_secret=<APP_SECRET>&revoke_token=<OLD>&access_token=<NEW>`.
  Emergency kill switch for every token of the system user: `DELETE /v27.0/<SYSTEM_USER_ID>/access_tokens` with an admin token.
- **Expiring tokens** (if your portfolio enforces them): generate with the 60-day option and refresh
  before expiry with
  `GET /v27.0/oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<CURRENT>&set_token_expires_in_60_days=true`,
  then update the env file and restart. This is a manual or scheduled step; tell us if this applies
  and a refresh job should be added to the service.
- **App Secret reset** invalidates webhook signatures immediately; update `WA_APP_SECRET` in the same
  change window. Meta retries undelivered webhooks for up to 7 days, so nothing is lost.
- Check quarterly: `whatsapp-webhook meta-check` (token validity, scopes, subscription) and the Graph
  version retirement schedule at https://developers.facebook.com/docs/graph-api/changelog/versions.

## 7. Limits that matter here

- Inbound media: images 5 MB, audio and video 16 MB, documents 100 MB, stickers 500 KB. Files above
  100 MB never arrive; Meta sends an `unsupported` message with error 131052, which the service
  forwards as an email stating the error.
- Webhook payloads are up to 3 MB and may batch many messages; deliveries are retried for up to 7
  days and duplicates are expected. The service de-duplicates on the WhatsApp message id.
- The sender's phone number may be absent from a payload (WhatsApp usernames and business-scoped
  user ids). The service then skips the CRM lookup and uses the display name and username.

## 8. Sources

- [Permissions](https://developers.facebook.com/documentation/business-messaging/whatsapp/permissions),
  [Access tokens](https://developers.facebook.com/documentation/business-messaging/whatsapp/access-tokens),
  [Get started](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started),
  [Create a webhook endpoint](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint),
  [Webhooks overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview),
  [Subscribed apps API](https://developers.facebook.com/documentation/business-messaging/whatsapp/reference/whatsapp-business-account/subscribed-apps-api),
  [Media](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media),
  [Account model evolution](https://developers.facebook.com/documentation/business-messaging/whatsapp/account-model-evolution),
  [Business-scoped user ids](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids),
  [App modes](https://developers.facebook.com/documentation/development/build-and-test/app-modes),
  [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog),
  [Introducing Graph API v27.0](https://developers.facebook.com/blog/post/2026/11/04/introducing-graph-api-v27-and-marketing-api-v27/).
- Business Help Center: [Add system users to your business portfolio](https://www.facebook.com/business/help/503306463479099),
  [System user security](https://www.facebook.com/business/help/327596604689624).
- Marketing API: [System users: install apps and generate tokens](https://developers.facebook.com/docs/marketing-api/system-users/install-apps-and-generate-tokens).
