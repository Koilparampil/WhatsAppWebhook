# Sending a customer a WhatsApp update by email

Only people whose address is on the approved list can do this. Ask the administrator to be added.
**Send it from your own mailbox**: not from a shared mailbox, not "on behalf of" someone else, and not by
an Outlook rule or a Power Automate flow. Such emails are ignored, and you get no answer.

## 1. Write a NEW email (not a reply, not a forward)

**To:** `whatsapp@vship2000.com`

**Subject:** `WA | <customer's phone number> | <booking number>`

```
WA | 650-555-1234 | BK12345
WA | +44 20 7946 0958 | BK12345
WA | 6505551234                    (booking number may be left out)
```

- US numbers: 10 digits, with spaces, dashes, dots or brackets if you like (`650 555 1234`,
  `(650) 555-1234`, `1-650-555-1234`). No slashes, letters or extensions.
- Any other country: start with `+` and the country code (`+44 ...`). Not `00` or `011`.
- Nothing may stand in front of `WA`. An email whose subject starts with `RE:` or `FW:` is never sent,
  so replying to an old update or to a confirmation can never send anything twice.

**Text of the email:**

```
Name:Maria
Message:
Your container has arrived at the port of Newark.
Customs release is expected on Friday.
##
```

- `Name:` is the **customer's** name, as it should appear in "Hello Maria,". Left empty, the customer
  sees "Hello there,".
- Everything between `Message:` and `##` is the update. Put `##` alone on its own line right after it.
- Your signature, disclaimers and anything else **below `##`** never reach the customer. Without the
  `##` line nothing is sent.
- Line breaks are joined into one paragraph (WhatsApp does not allow line breaks in this message).
- At most 820 characters (so that the whole WhatsApp message stays within WhatsApp's limit). No bold or
  colours: the customer gets plain text.
- **Only booking or shipment information. Never offers, promotions or advertising.** Promotional text can
  make WhatsApp re-file the message type as marketing, and marketing messages are not delivered to US
  numbers at all.

## 2. What the customer receives

```
Hello Maria, VShip has an update:
Your container has arrived at the port of Newark. Customs release is expected on Friday.
Please either reply to this message or send us an email at Elizabeth@vship2000.com if you have any Questions
```

If the customer answers on WhatsApp, the answer arrives by email to the usual WhatsApp inbox, as every
WhatsApp message to VShip does today. It does not come to you personally.

## 3. Files

Attach files normally. What WhatsApp lets through depends on whether the customer wrote to VShip on
WhatsApp in the last 24 hours (a WhatsApp rule):

| | Customer wrote in the last 24 hours | Otherwise |
|---|---|---|
| PDF | sent as its own message after the update | sent only if the administrator has set up the document template (and, if that template shows the booking number, only when your subject has one); otherwise your confirmation says why |
| Word, Excel, PowerPoint, JPEG/PNG pictures, MP4 video | sent as its own message after the update | **not sent**: save it as a PDF and send again (sending again also sends the update text again) |
| Anything else, pictures pasted into the text, attached emails, OneDrive/SharePoint links ("cloud attachments") | never sent | never sent |

Size limits: 100 MB for documents, 5 MB for pictures, 16 MB for videos (email limits are usually
lower). Your confirmation always lists which files went and which did not, and why.

A picture that your mail program shows inside the text of the email (a pasted screenshot, a signature
logo, and on an iPhone often an attached photo too) counts as part of the text and is never sent: if a
picture must reach the customer, attach it from Outlook on a computer and check the confirmation. A PDF,
Word, Excel, PowerPoint or video file always counts as attached, even when your mail program shows it
inside the text.

## 4. The email you get back (within about a minute)

| Subject starts with | Meaning | What to do |
|---|---|---|
| `WhatsApp update sent` | WhatsApp accepted it. Shows exactly what the customer got, and whether the number belongs to a customer in VShipCRM | Check the number and name once more |
| `WhatsApp update NOT sent` | Nothing reached the customer. The email says why | Do what the email says under "What to do next". For a mistake in your email (subject, phone number, the `Name:` / `Message:` / `##` lines, a message that is too long): fix it and send a **new** email. For other reasons (for example the WhatsApp template is not ready, or test mode is on) the email says whether to send it again later, contact the customer another way, or ask the administrator |
| `WhatsApp update MAY NOT have been sent` | WhatsApp did not answer clearly. It is never re-sent automatically, so the customer never gets it twice | Look at the customer's chat in the WhatsApp Business app on the business phone; send again only if it is not there |
| `WhatsApp could not deliver your update` | WhatsApp accepted it but could not deliver it (for example: no WhatsApp on that number, an old WhatsApp version, a billing problem); the email gives WhatsApp's reason | Contact the customer another way, or tell the administrator if the email says so |
| `DRY RUN` | The service is in a trial run; nothing went to the customer. The email shows what the customer would have received | Check that the text is what you meant to send. If the customer needs the update now, contact them another way |

If no email comes back within 5 minutes: check that the subject starts exactly with `WA |`, that you
wrote to `whatsapp@vship2000.com` from your own mailbox, and tell the administrator (your email may have
been held by the spam filter, or your address may not be on the list).

## 5. Please remember

- Sending the same email twice sends the customer the message twice.
- The service cannot change or take back a WhatsApp message once it is sent.
- Only message customers who agreed to receive WhatsApp messages from VShip. If a customer asks you to
  stop, stop, and tell the administrator.
