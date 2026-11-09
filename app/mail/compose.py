"""Build the outgoing email(s) for one WhatsApp message.

One WhatsApp message normally becomes one email. When the attached media would push the encoded
email over ``MAIL_MAX_MESSAGE_BYTES``, the file is split into numbered parts (``name.001``,
``name.002`` …) and each part is sent in its own email with rebuild instructions.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate
from pathlib import Path
from zoneinfo import ZoneInfo

import phonenumbers

from app.config import Settings
from app.crm.directory import LookupResult, LookupStatus
from app.whatsapp.media import DownloadedMedia
from app.whatsapp.parser import InboundMessage

BASE64_RAW_LINE = 57  # raw bytes per 76-char base64 line
BASE64_ENCODED_LINE = 78  # 76 chars + CRLF
ATTACHMENT_HEADER_OVERHEAD = 1024
SAFETY_MARGIN = 8 * 1024
MIN_CHUNK_BYTES = 512 * 1024
# Space reserved for headers and the text/HTML bodies when splitting. Using a fixed reserve (rather
# than the measured body size) keeps the chunk boundaries identical across retries, so a message
# whose later parts failed can resume without re-sending or mis-cutting earlier parts.
BODY_RESERVE = 256 * 1024


class ComposeError(Exception):
    """The message cannot be turned into an email that fits the configured limits."""


@dataclass(slots=True)
class OutgoingEmail:
    message: EmailMessage
    part_index: int
    part_count: int
    attachment_name: str | None


def encoded_attachment_size(raw_size: int) -> int:
    """Bytes a base64 attachment of ``raw_size`` occupies inside the MIME message."""
    lines = math.ceil(raw_size / BASE64_RAW_LINE) if raw_size else 0
    return lines * BASE64_ENCODED_LINE + ATTACHMENT_HEADER_OVERHEAD


def chunk_size_for(limit: int, base_size: int = 0) -> int:
    """Largest raw chunk whose base64 form fits in ``limit`` next to a message body.

    Deterministic for a given ``limit``: the body is assumed to take at most ``BODY_RESERVE`` bytes.
    ``base_size`` is only validated against that reserve.
    """
    if base_size > BODY_RESERVE:
        raise ComposeError(f"email body ({base_size} bytes) exceeds the {BODY_RESERVE}-byte reserve")
    available = limit - BODY_RESERVE - ATTACHMENT_HEADER_OVERHEAD - SAFETY_MARGIN
    lines = available // BASE64_ENCODED_LINE
    return max(lines * BASE64_RAW_LINE, MIN_CHUNK_BYTES)


def _clean_header_text(value: str | None) -> str:
    return re.sub(r"[\r\n\t]+", " ", value or "").strip()


def _message_id(wamid: str, domain: str, part: int | None = None) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", wamid).strip("-") or "unknown"
    suffix = f".p{part}" if part else ""
    return f"<wa.{token}{suffix}@{domain}>"


def format_phone_for_humans(e164: str | None) -> str | None:
    if not e164:
        return None
    try:
        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164


def _human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def _local_time(timestamp: int, tz_name: str) -> str:
    if not timestamp:
        return "unknown"
    dt = datetime.fromtimestamp(timestamp, tz=UTC).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%a %d %b %Y %H:%M:%S %Z")


# ----------------------------------------------------------------------------- body sections
def _sender_lines(msg: InboundMessage, settings: Settings) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = [("From", msg.sender_label)]
    phone = format_phone_for_humans(msg.phone_e164)
    lines.append(("Phone", phone or "not shared by WhatsApp (sender uses a username or privacy setting)"))
    if msg.username:
        lines.append(("Username", f"@{msg.username}"))
    if msg.from_bsuid:
        lines.append(("WhatsApp user id", msg.from_bsuid))
    lines.append(("Received", _local_time(msg.timestamp, settings.mail_timezone)))
    if msg.business_display_number:
        lines.append(("Sent to business number", "+" + re.sub(r"\D", "", msg.business_display_number)))
    lines.append(("Message type", msg.type))
    return lines


def _content_lines(msg: InboundMessage) -> list[str]:
    out: list[str] = []
    if msg.type == "text":
        out.append(msg.text or "(empty message)")
    elif msg.media is not None:
        kind = "voice note" if msg.media.kind == "audio" and msg.media.voice else msg.media.kind
        out.append(
            f"The sender attached a {kind}"
            + (f" ({msg.media.mime_type})" if msg.media.mime_type else "")
            + "."
        )
        if msg.media.filename:
            out.append(f"File name: {msg.media.filename}")
        if msg.media.caption:
            out.append("")
            out.append(f"Caption: {msg.media.caption}")
    elif msg.type == "location" and msg.location:
        loc = msg.location
        if loc.name:
            out.append(f"Place: {loc.name}")
        if loc.address:
            out.append(f"Address: {loc.address}")
        if loc.latitude is not None and loc.longitude is not None:
            out.append(f"Coordinates: {loc.latitude}, {loc.longitude}")
        if loc.maps_url:
            out.append(f"Map: {loc.maps_url}")
        if loc.url:
            out.append(f"Link: {loc.url}")
    elif msg.type == "contacts":
        for card in msg.contacts:
            out.append(f"Contact: {card.formatted_name or '(no name)'}")
            if card.org:
                out.append(f"  Organisation: {card.org}")
            for p in card.phones:
                out.append(f"  Phone: {p}")
            for e in card.emails:
                out.append(f"  Email: {e}")
    elif msg.type == "interactive" and msg.interactive:
        itype = msg.interactive.get("type") or "interactive"
        out.append(f"Interactive reply ({itype}):")
        for key in ("title", "id", "description", "name", "body"):
            if msg.interactive.get(key):
                out.append(f"  {key}: {msg.interactive[key]}")
        if msg.interactive.get("response_json"):
            out.append(f"  response: {msg.interactive['response_json']}")
    elif msg.type == "button" and msg.button:
        out.append(f"Button pressed: {msg.button.get('text') or ''}")
        if msg.button.get("payload"):
            out.append(f"Payload: {msg.button['payload']}")
    elif msg.type == "reaction" and msg.reaction:
        emoji = msg.reaction.get("emoji")
        out.append(f"Reaction: {emoji}" if emoji else "Reaction removed")
        if msg.reaction.get("message_id"):
            out.append(f"On message: {msg.reaction['message_id']}")
    elif msg.type == "order" and msg.order:
        out.append(f"Order from catalog {msg.order.get('catalog_id') or '?'}")
        if msg.order.get("text"):
            out.append(f"Note: {msg.order['text']}")
        for item in msg.order.get("product_items") or []:
            out.append(
                f"  - {item.get('product_retailer_id', '?')} x{item.get('quantity', '?')}"
                f" @ {item.get('item_price', '?')} {item.get('currency', '')}".rstrip()
            )
    elif msg.type == "system" and msg.system:
        out.append(msg.system.get("body") or "System notification")
        for key in ("type", "wa_id", "new_wa_id", "identity"):
            if msg.system.get(key):
                out.append(f"  {key}: {msg.system[key]}")
    elif msg.type == "request_welcome":
        out.append("The sender opened a conversation with the business (no text yet).")
    elif msg.type == "unsupported":
        out.append(
            "WhatsApp could not deliver this message type to the webhook (unsupported by the Cloud API)."
        )
    else:
        out.append(f"Message of type '{msg.type}' (not rendered; raw payload is in the details section).")
    for err in msg.errors:
        out.append(f"WhatsApp error: {err.describe()}")
    return out


def _crm_lines(lookup: LookupResult) -> list[str]:
    out: list[str] = []
    searched = ", ".join(lookup.entities_searched) or "none"
    if lookup.status is LookupStatus.MATCHED and lookup.best:
        best = lookup.best
        how = "exact match" if best.exact else "last-10-digit match"
        out.append(f'Match: {best.record.label} ({how} on {best.matched_field} "{best.matched_raw}")')
        out.append("Replies to this email go to: " + ", ".join(lookup.reply_to))
        others = lookup.candidates[1:]
        if others:
            out.append(
                f"Other CRM records with this number ({len(others)}): "
                + "; ".join(c.record.label for c in others)
            )
    elif lookup.status is LookupStatus.NO_PHONE:
        out.append("No lookup possible: WhatsApp did not include the sender's phone number.")
        out.append("Replies to this email will NOT reach the sender.")
    elif lookup.status is LookupStatus.NO_MATCH:
        out.append(f"No match: {lookup.reason} (searched: {searched}).")
        out.append("Replies to this email will NOT reach the sender.")
    elif lookup.status is LookupStatus.ERROR:
        out.append(f"Lookup failed: {lookup.reason}")
        out.append("Replies to this email will NOT reach the sender.")
    else:
        out.append("CRM lookup is disabled on this server.")
    if lookup.index_age_seconds is not None:
        out.append(f"(CRM index age: {int(lookup.index_age_seconds // 60)} min)")
    return out


def _attachment_lines(
    media: DownloadedMedia | None, part_index: int, part_count: int, chunk_names: list[str]
) -> list[str]:
    if media is None:
        return []
    out = [f"{media.filename} ({_human_size(media.size)}, {media.mime_type})"]
    if media.integrity_verified is False:
        out.append("Warning: the file's checksum did not match the value WhatsApp reported.")
    if part_count > 1:
        out.append("")
        out.append(
            f"This file is too large for one email, so it was split into {part_count} parts."
            f" This email carries part {part_index} of {part_count}: {chunk_names[part_index - 1]}"
        )
        out.append("Save all parts to one folder, then rebuild the original file:")
        out.append("  Windows (Command Prompt):")
        out.append("    copy /b " + "+".join(f'"{n}"' for n in chunk_names) + f' "{media.filename}"')
        out.append("  macOS / Linux (Terminal):")
        out.append("    cat " + " ".join(f"'{n}'" for n in chunk_names) + f" > '{media.filename}'")
        out.append("  Or right-click the .001 file in 7-Zip and choose 'Combine files'.")
    return out


def _details_lines(msg: InboundMessage) -> list[str]:
    out = [f"WhatsApp message id: {msg.wamid}"]
    if msg.business_phone_number_id:
        out.append(f"Business phone number id: {msg.business_phone_number_id}")
    if msg.context:
        if msg.context.quoted_message_id:
            out.append(f"In reply to message: {msg.context.quoted_message_id}")
        if msg.context.forwarded or msg.context.frequently_forwarded:
            out.append(
                "Forwarded message" + (" (frequently forwarded)" if msg.context.frequently_forwarded else "")
            )
        if msg.context.referred_product:
            out.append(f"Referred product: {msg.context.referred_product}")
    if msg.referral:
        ref = msg.referral
        out.append("Came from an ad / link:")
        for key, value in (
            ("headline", ref.headline),
            ("body", ref.body),
            ("source", ref.source_type),
            ("url", ref.source_url),
            ("click id", ref.ctwa_clid),
        ):
            if value:
                out.append(f"  {key}: {value}")
    return out


def _render_text(sections: list[tuple[str, list[str]]], header: list[tuple[str, str]]) -> str:
    width = max(len(k) for k, _ in header)
    lines = ["WhatsApp message"]
    lines.extend(f"{k.ljust(width)} : {v}" for k, v in header)
    for title, body in sections:
        if not body:
            continue
        lines.append("")
        lines.append(f"----- {title} -----")
        lines.extend(body)
    lines.append("")
    lines.append("This email was generated automatically by the WhatsApp email forwarder.")
    return "\n".join(lines) + "\n"


def _render_html(sections: list[tuple[str, list[str]]], header: list[tuple[str, str]]) -> str:
    esc = html.escape
    rows = "".join(
        f'<tr><th style="text-align:left;padding:2px 10px 2px 0;color:#555;white-space:nowrap">{esc(k)}</th>'
        f'<td style="padding:2px 0">{esc(v)}</td></tr>'
        for k, v in header
    )
    blocks = []
    for title, body in sections:
        if not body:
            continue
        content = "<br>".join(esc(line) if line else "&nbsp;" for line in body)
        content = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', content)
        blocks.append(
            f'<h3 style="margin:18px 0 6px;font-size:14px;color:#222">{esc(title)}</h3>'
            f'<div style="white-space:pre-wrap;font-family:inherit">{content}</div>'
        )
    return (
        '<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#111">'
        '<h2 style="margin:0 0 10px;font-size:16px">WhatsApp message</h2>'
        f'<table style="border-collapse:collapse">{rows}</table>'
        + "".join(blocks)
        + '<p style="margin-top:20px;color:#777;font-size:12px">'
        "This email was generated automatically by the WhatsApp email forwarder.</p></body></html>"
    )


# ----------------------------------------------------------------------------- public API
def build_emails(
    msg: InboundMessage,
    lookup: LookupResult,
    media: DownloadedMedia | None,
    settings: Settings,
) -> list[OutgoingEmail]:
    """Return the email(s) to send for ``msg``; more than one only when media must be split."""
    chunk_names: list[str] = []
    part_count = 1
    chunk_size = 0
    if media is not None:
        base = _assemble(msg, lookup, media, settings, 1, 1, [])
        base_size = len(base.as_bytes())
        if base_size + encoded_attachment_size(media.size) + SAFETY_MARGIN <= settings.mail_max_message_bytes:
            part_count = 1
        elif not settings.mail_split_large_media:
            raise ComposeError(
                f"attachment {media.filename} ({media.size} bytes) exceeds MAIL_MAX_MESSAGE_BYTES"
                f" ({settings.mail_max_message_bytes}) and splitting is disabled"
            )
        else:
            chunk_size = chunk_size_for(settings.mail_max_message_bytes, base_size)
            part_count = max(1, math.ceil(media.size / chunk_size))
            chunk_names = [f"{media.filename}.{i:03d}" for i in range(1, part_count + 1)]

    emails: list[OutgoingEmail] = []
    for index in range(1, part_count + 1):
        message = _assemble(msg, lookup, media, settings, index, part_count, chunk_names)
        attachment_name: str | None = None
        if media is not None:
            if part_count == 1:
                data = Path(media.path).read_bytes()
                attachment_name = media.filename
                mime = (media.mime_type or "application/octet-stream").split(";")[0].strip().lower()
                maintype, _, subtype = mime.partition("/")
                if not subtype or not re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*", maintype):
                    maintype, subtype = "application", "octet-stream"
                message.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment_name)
            else:
                attachment_name = chunk_names[index - 1]
                with Path(media.path).open("rb") as fh:
                    fh.seek((index - 1) * chunk_size)
                    data = fh.read(chunk_size)
                message.add_attachment(
                    data, maintype="application", subtype="octet-stream", filename=attachment_name
                )
        emails.append(
            OutgoingEmail(
                message=message, part_index=index, part_count=part_count, attachment_name=attachment_name
            )
        )
    return emails


def _assemble(
    msg: InboundMessage,
    lookup: LookupResult,
    media: DownloadedMedia | None,
    settings: Settings,
    part_index: int,
    part_count: int,
    chunk_names: list[str],
) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    domain = settings.mail_from_domain
    display = _clean_header_text(settings.mail_from_name_template.format(display_name=msg.sender_label))
    message["From"] = formataddr((display, settings.mail_from))
    message["To"] = ", ".join(settings.mail_to_list)
    if lookup.status is LookupStatus.MATCHED and lookup.reply_to:
        message["Reply-To"] = ", ".join(lookup.reply_to)
    elif settings.mail_fallback_reply_to:
        message["Reply-To"] = settings.mail_fallback_reply_to

    phone = format_phone_for_humans(msg.phone_e164)
    who = msg.sender_label + (f" ({phone})" if phone and phone != msg.sender_label else "")
    subject = f"{settings.mail_subject_prefix} {who}: {msg.summary()}".strip()
    if part_count > 1:
        subject += f" [part {part_index}/{part_count}]"
    message["Subject"] = _clean_header_text(subject)
    message["Date"] = formatdate(msg.timestamp or None, localtime=False)

    first_id = _message_id(msg.wamid, domain)
    message["Message-ID"] = first_id if part_index == 1 else _message_id(msg.wamid, domain, part_index)
    references: list[str] = []
    if msg.context and msg.context.quoted_message_id:
        references.append(_message_id(msg.context.quoted_message_id, domain))
    if part_index > 1:
        references.append(first_id)
    if references:
        message["In-Reply-To"] = references[-1]
        message["References"] = " ".join(references)

    message["X-WhatsApp-Message-Id"] = _clean_header_text(msg.wamid)
    message["X-WhatsApp-From"] = msg.phone_e164 or "unknown"
    if msg.from_bsuid:
        message["X-WhatsApp-BSUID"] = _clean_header_text(msg.from_bsuid)
    message["X-WhatsApp-Type"] = _clean_header_text(msg.type)
    if lookup.status is LookupStatus.MATCHED and lookup.best:
        message["X-WhatsApp-CRM-Match"] = (
            f"{lookup.best.record.entity.lower()}:{lookup.best.record.record_id}"
        )
    else:
        message["X-WhatsApp-CRM-Match"] = lookup.status.value
    if part_count > 1:
        message["X-WhatsApp-Part"] = f"{part_index}/{part_count}"

    header = _sender_lines(msg, settings)
    sections = [
        ("Message", _content_lines(msg)),
        ("VShipCRM", _crm_lines(lookup)),
        ("Attachment", _attachment_lines(media, part_index, part_count, chunk_names)),
        ("Details", _details_lines(msg)),
    ]
    message.set_content(_render_text(sections, header))
    message.add_alternative(_render_html(sections, header), subtype="html")
    return message


def build_alert_email(subject: str, body: str, settings: Settings) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr(("WhatsApp email forwarder", settings.mail_from))
    message["To"] = ", ".join(settings.alert_to_list)
    message["Subject"] = _clean_header_text(f"{settings.mail_subject_prefix} ALERT: {subject}")
    message["Date"] = formatdate(localtime=False)
    message["X-WhatsApp-Alert"] = "dead-letter"
    message.set_content(body)
    return message
