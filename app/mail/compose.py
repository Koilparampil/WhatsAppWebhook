"""Build the outgoing email(s) for one WhatsApp message.

One WhatsApp message normally becomes one email. When the attached media would push the encoded
email over ``MAIL_MAX_MESSAGE_BYTES``, the file is split into numbered parts (``name.001``,
``name.002`` …) and each part is sent in its own email with rebuild instructions.
"""

from __future__ import annotations

import base64
import html
import json
import math
import re
import shlex
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage, MIMEPart
from email.policy import SMTP
from email.utils import formataddr, formatdate
from pathlib import Path
from zoneinfo import ZoneInfo

import phonenumbers

from app.config import Settings
from app.crm.directory import LookupResult, LookupStatus
from app.whatsapp.media import DownloadedMedia
from app.whatsapp.parser import MEDIA_TYPES as MEDIA_KINDS
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
ATTACHABLE_MAINTYPES = {"image", "audio", "video", "text", "application", "font", "model"}


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


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_format_chars(value: str) -> str:
    """Remove Unicode format characters (category Cf: bidi overrides, zero-width joiners, BOM...).

    They are invisible but let a sender reverse how a name or file extension is displayed.
    """
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cf")


def _clean_header_text(value: str | None) -> str:
    """Make sender-controlled text safe for a header: one line, no control or format characters.

    ``str.split()`` also folds Unicode line/paragraph separators (U+2028, U+2029, U+0085, VT, FF…)
    which the email package would otherwise reject as multi-line header values.
    """
    collapsed = " ".join((value or "").split())
    return strip_format_chars(_CONTROL_CHARS.sub("", collapsed)).strip()


def _display_name_safe(value: str) -> str:
    """A From display name must not look like an address.

    Angle brackets, quotes and backslashes are dropped, and an ``@`` inside a word (``a@b.com``)
    becomes `` at ``; a leading ``@`` (the WhatsApp username marker) is kept.
    """
    value = re.sub(r'[<>"\\]', " ", value)
    value = re.sub(r"(?<=\S)@(?=\S)", " at ", value)
    return _clean_header_text(value)


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
    # "Sender", not "From": some mail stores rewrite body lines that start with "From " to ">From "
    lines: list[tuple[str, str]] = [("Sender", msg.sender_label)]
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
        out.append(f"Message of type '{msg.type}' (not rendered; the raw message is in the details section).")
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
        out.append("    " + _windows_rebuild_command(media.filename, chunk_names))
        out.append("  macOS / Linux (Terminal):")
        out.append("    " + _posix_rebuild_command(media.filename, chunk_names))
        out.append("  Or right-click the .001 file in 7-Zip and choose 'Combine files'.")
    return out


MAX_LISTED_PARTS = 12  # beyond this the instructions use a wildcard so the body stays small


def _cmd_quote(name: str) -> str:
    """Quote for cmd.exe: safe_filename already removes the characters cmd cannot quote (", %)."""
    return '"' + name.replace('"', "").replace("%", "_") + '"'


def _windows_rebuild_command(filename: str, chunk_names: list[str]) -> str:
    if len(chunk_names) <= MAX_LISTED_PARTS:
        return "copy /b " + "+".join(_cmd_quote(n) for n in chunk_names) + " " + _cmd_quote(filename)
    return "copy /b " + _cmd_quote(filename + ".???") + " " + _cmd_quote(filename)


def _posix_rebuild_command(filename: str, chunk_names: list[str]) -> str:
    if len(chunk_names) <= MAX_LISTED_PARTS:
        return "cat " + " ".join(shlex.quote(n) for n in chunk_names) + " > " + shlex.quote(filename)
    return "cat " + shlex.quote(filename) + ".[0-9][0-9][0-9] > " + shlex.quote(filename)


KNOWN_TYPES = {
    "text", "image", "video", "audio", "document", "sticker", "location", "contacts", "interactive",
    "button", "reaction", "order", "system", "request_welcome", "unsupported",
}  # fmt: skip
RAW_DETAILS_LIMIT = 2000


def _details_lines(msg: InboundMessage) -> list[str]:
    out = [f"WhatsApp message id: {msg.wamid}"]
    if msg.business_phone_number_id:
        out.append(f"Business phone number id: {msg.business_phone_number_id}")
    if msg.type not in KNOWN_TYPES or (msg.media is None and msg.type in MEDIA_KINDS):
        raw = json.dumps(msg.raw, ensure_ascii=False, sort_keys=True)
        if len(raw) > RAW_DETAILS_LIMIT:
            raw = raw[:RAW_DETAILS_LIMIT] + " …(truncated)"
        out.append(f"Raw message: {raw}")
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


_MAPS_LINK = re.compile(r"(https://www\.google\.com/maps/search/\?api=1&amp;query=[-0-9.,]+)")


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
        # Only the map link the service generated becomes clickable; sender-supplied URLs stay text.
        content = _MAPS_LINK.sub(r'<a href="\1">\1</a>', content)
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
@dataclass(slots=True)
class EmailPlan:
    """Everything needed to build any single part of a message's email(s) on demand."""

    msg: InboundMessage
    lookup: LookupResult
    media: DownloadedMedia | None
    settings: Settings
    part_count: int
    chunk_size: int
    chunk_names: list[str]

    @property
    def signature(self) -> str:
        """Identifies how the media was cut; a resumed send must use the same cut."""
        size = self.media.size if self.media is not None else 0
        return f"{self.part_count}x{self.chunk_size}/{size}"


def plan_emails(
    msg: InboundMessage,
    lookup: LookupResult,
    media: DownloadedMedia | None,
    settings: Settings,
) -> EmailPlan:
    """Decide how many emails ``msg`` needs (more than one only when media must be split).

    The single-or-split decision and the chunk size depend only on the file size and
    ``MAIL_MAX_MESSAGE_BYTES`` (never on the measured body, which changes between attempts with the
    CRM result), so a retry after a partial send reaches the same plan.
    """
    chunk_names: list[str] = []
    part_count = 1
    chunk_size = 0
    limit = settings.mail_max_message_bytes
    if media is not None:
        if BODY_RESERVE + encoded_attachment_size(media.size) + SAFETY_MARGIN <= limit:
            part_count = 1
        elif not settings.mail_split_large_media:
            raise ComposeError(
                f"attachment {media.filename} ({media.size} bytes) exceeds MAIL_MAX_MESSAGE_BYTES"
                f" ({limit}) and splitting is disabled"
            )
        else:
            chunk_size = chunk_size_for(limit)
            part_count = max(1, math.ceil(media.size / chunk_size))
            chunk_names = [f"{media.filename}.{i:03d}" for i in range(1, part_count + 1)]
        # The body of the real first part (with the rebuild instructions) must fit the reserve.
        probe = _assemble(msg, lookup, media, settings, 1, part_count, chunk_names)
        body_size = len(probe.as_bytes())
        if body_size > BODY_RESERVE:
            raise ComposeError(f"email body ({body_size} bytes) exceeds the {BODY_RESERVE}-byte reserve")
    return EmailPlan(msg, lookup, media, settings, part_count, chunk_size, chunk_names)


ENCODE_BLOCK_BYTES = BASE64_RAW_LINE * 2048  # whole 76-character lines per block


def encode_file_slice_base64(path: Path, offset: int, length: int) -> str:
    """Base64 text (76-character lines) of ``length`` bytes at ``offset`` of ``path``.

    Encoding block by block straight from the spooled file avoids holding the raw bytes and the
    email package's per-line list at the same time (it costs about twice the encoded size at peak,
    instead of roughly four times).
    """
    pieces: list[str] = []
    remaining = length
    with path.open("rb") as fh:
        fh.seek(offset)
        while remaining > 0:
            block = fh.read(min(ENCODE_BLOCK_BYTES, remaining))
            if not block:
                raise ComposeError(f"spooled media ended early ({remaining} bytes missing)")
            remaining -= len(block)
            pieces.append(base64.encodebytes(block).decode("ascii"))
    return "".join(pieces)


def _attach_base64(message: EmailMessage, encoded: str, maintype: str, subtype: str, filename: str) -> None:
    """Attach already-encoded data; same headers and RFC 2231 file name encoding as add_attachment."""
    part = MIMEPart(policy=message.policy)
    part["Content-Type"] = f"{maintype}/{subtype}"
    part["Content-Transfer-Encoding"] = "base64"
    part["Content-Disposition"] = "attachment"
    part.set_param("filename", filename, header="Content-Disposition")
    part.set_payload(encoded)
    if message.get_content_type() != "multipart/mixed":
        message.make_mixed()
    message.attach(part)


def build_part(plan: EmailPlan, index: int) -> OutgoingEmail:
    """Build email ``index`` (1-based) of the plan. Only this part's bytes are held in memory."""
    if not 1 <= index <= plan.part_count:
        raise ComposeError(f"part {index} out of range 1..{plan.part_count}")
    media = plan.media
    try:
        message = _assemble(
            plan.msg, plan.lookup, media, plan.settings, index, plan.part_count, plan.chunk_names
        )
        attachment_name: str | None = None
        if media is not None:
            if plan.part_count == 1:
                attachment_name = media.filename
                mime = (media.mime_type or "application/octet-stream").split(";")[0].strip().lower()
                maintype, _, subtype = mime.partition("/")
                if maintype not in ATTACHABLE_MAINTYPES or not re.fullmatch(
                    r"[a-z0-9][a-z0-9!#$&^_.+-]*", subtype
                ):
                    # message/* and multipart/* cannot carry base64; unknown types become binary
                    maintype, subtype = "application", "octet-stream"
                offset, length = 0, media.size
            else:
                attachment_name = plan.chunk_names[index - 1]
                maintype, subtype = "application", "octet-stream"
                offset = (index - 1) * plan.chunk_size
                length = min(plan.chunk_size, media.size - offset)
            encoded = encode_file_slice_base64(Path(media.path), offset, length)
            _attach_base64(message, encoded, maintype, subtype, attachment_name)
            del encoded
    except ValueError as exc:  # the email package refuses a header or body it cannot represent
        raise ComposeError(f"could not assemble email: {exc}") from exc
    return OutgoingEmail(
        message=message, part_index=index, part_count=plan.part_count, attachment_name=attachment_name
    )


def build_emails(
    msg: InboundMessage,
    lookup: LookupResult,
    media: DownloadedMedia | None,
    settings: Settings,
) -> list[OutgoingEmail]:
    """Convenience wrapper that builds every part at once (tests and small messages)."""
    plan = plan_emails(msg, lookup, media, settings)
    return [build_part(plan, index) for index in range(1, plan.part_count + 1)]


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
    display = _clean_header_text(
        settings.mail_from_name_template.format(display_name=_display_name_safe(msg.sender_label))
    )
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
