"""Normalise WhatsApp Cloud API webhook payloads into :class:`InboundMessage` objects.

The parser is deliberately tolerant: Meta adds fields over time, phone numbers may be absent
(username / business-scoped user id era), and unknown message types are still forwarded.
Delivery receipts (``value.statuses``) are counted but never turned into messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MEDIA_TYPES = {"image", "video", "audio", "document", "sticker"}


@dataclass(slots=True)
class MediaRef:
    kind: str
    id: str
    mime_type: str | None = None
    sha256: str | None = None
    filename: str | None = None
    caption: str | None = None
    voice: bool = False
    animated: bool = False


@dataclass(slots=True)
class Location:
    latitude: float | None
    longitude: float | None
    name: str | None = None
    address: str | None = None
    url: str | None = None

    @property
    def maps_url(self) -> str | None:
        if self.latitude is None or self.longitude is None:
            return None
        return f"https://www.google.com/maps/search/?api=1&query={self.latitude},{self.longitude}"


@dataclass(slots=True)
class ContactCard:
    formatted_name: str | None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    org: str | None = None


@dataclass(slots=True)
class Context:
    quoted_message_id: str | None = None
    quoted_from: str | None = None
    forwarded: bool = False
    frequently_forwarded: bool = False
    referred_product: dict[str, Any] | None = None


@dataclass(slots=True)
class Referral:
    source_url: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    headline: str | None = None
    body: str | None = None
    media_type: str | None = None
    ctwa_clid: str | None = None


@dataclass(slots=True)
class MessageError:
    code: int | None
    title: str | None
    message: str | None = None
    details: str | None = None

    def describe(self) -> str:
        parts = [
            p for p in (f"code {self.code}" if self.code is not None else None, self.title, self.details) if p
        ]
        return " - ".join(parts) or (self.message or "unknown error")


@dataclass(slots=True)
class InboundMessage:
    wamid: str
    type: str
    timestamp: int
    business_phone_number_id: str | None
    business_display_number: str | None
    from_phone: str | None
    from_bsuid: str | None
    display_name: str | None
    username: str | None
    text: str | None = None
    media: MediaRef | None = None
    location: Location | None = None
    contacts: list[ContactCard] = field(default_factory=list)
    interactive: dict[str, Any] | None = None
    button: dict[str, Any] | None = None
    reaction: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    system: dict[str, Any] | None = None
    referral: Referral | None = None
    context: Context | None = None
    errors: list[MessageError] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def phone_e164(self) -> str | None:
        digits = "".join(ch for ch in (self.from_phone or "") if ch.isdigit())
        return f"+{digits}" if digits else None

    @property
    def sender_label(self) -> str:
        """Best human label for the sender: display name, else username, else phone, else BSUID."""
        if self.display_name:
            return self.display_name
        if self.username:
            return f"@{self.username}"
        if self.phone_e164:
            return self.phone_e164
        if self.from_bsuid:
            return f"WhatsApp user {self.from_bsuid}"
        return "Unknown WhatsApp user"

    def summary(self, max_len: int = 80) -> str:
        """One-line description used in the email subject."""
        text = self._summary_text()
        text = " ".join(text.split())
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip() + "…"
        return text

    def _summary_text(self) -> str:
        t = self.type
        if t == "text":
            return self.text or "(empty message)"
        if self.media is not None:
            label = {
                "image": "sent an image",
                "video": "sent a video",
                "audio": "sent a voice note" if self.media.voice else "sent an audio file",
                "document": f"sent a document: {self.media.filename}"
                if self.media.filename
                else "sent a document",
                "sticker": "sent a sticker",
            }[self.media.kind]
            return f"{label}: {self.media.caption}" if self.media.caption else label
        if t == "location" and self.location:
            where = self.location.name or self.location.address
            return f"shared a location: {where}" if where else "shared a location"
        if t == "contacts":
            n = len(self.contacts)
            names = ", ".join(c.formatted_name for c in self.contacts if c.formatted_name)
            return f"shared {n} contact{'s' if n != 1 else ''}" + (f": {names}" if names else "")
        if t == "interactive" and self.interactive:
            title = (
                self.interactive.get("title") or self.interactive.get("name") or self.interactive.get("body")
            )
            return f"replied: {title}" if title else "sent an interactive reply"
        if t == "button" and self.button:
            return f"pressed: {self.button.get('text') or self.button.get('payload')}"
        if t == "reaction" and self.reaction:
            emoji = self.reaction.get("emoji")
            return f"reacted {emoji}" if emoji else "removed a reaction"
        if t == "order" and self.order:
            items = self.order.get("product_items") or []
            return f"placed an order ({len(items)} item{'s' if len(items) != 1 else ''})"
        if t == "system" and self.system:
            return self.system.get("body") or "system notification"
        if t == "request_welcome":
            return "started a conversation"
        if t == "unsupported":
            return "sent an unsupported message type"
        return f"sent a {t} message"


@dataclass(slots=True)
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: int = 0
    skipped_messages: int = 0
    other_fields: list[str] = field(default_factory=list)
    value_errors: list[MessageError] = field(default_factory=list)
    object_type: str | None = None


# ----------------------------------------------------------------------------- helpers
def _str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_error(obj: Any) -> MessageError:
    if not isinstance(obj, dict):
        return MessageError(code=None, title=None, message=_str(obj))
    data = obj.get("error_data") if isinstance(obj.get("error_data"), dict) else {}
    return MessageError(
        code=_int(obj.get("code"), 0) or None,
        title=_str(obj.get("title")),
        message=_str(obj.get("message")),
        details=_str(data.get("details")) if data else None,
    )


def _parse_contact_card(obj: dict[str, Any]) -> ContactCard:
    name = obj.get("name") if isinstance(obj.get("name"), dict) else {}
    formatted = _str(name.get("formatted_name")) if name else None
    if not formatted and name:
        formatted = " ".join(p for p in (name.get("first_name"), name.get("last_name")) if p) or None
    phones: list[str] = []
    for p in obj.get("phones") or []:
        if isinstance(p, dict):
            number = p.get("phone") or p.get("wa_id")
            if number:
                phones.append(str(number))
    emails: list[str] = []
    for e in obj.get("emails") or []:
        if isinstance(e, dict) and e.get("email"):
            emails.append(str(e["email"]))
    org = obj.get("org") if isinstance(obj.get("org"), dict) else {}
    return ContactCard(
        formatted_name=formatted, phones=phones, emails=emails, org=_str(org.get("company")) if org else None
    )


def _parse_media(kind: str, obj: dict[str, Any]) -> MediaRef | None:
    media_id = obj.get("id")
    if not media_id:
        return None
    return MediaRef(
        kind=kind,
        id=str(media_id),
        mime_type=_str(obj.get("mime_type")),
        sha256=_str(obj.get("sha256")),
        filename=_str(obj.get("filename")),
        caption=_str(obj.get("caption")),
        voice=bool(obj.get("voice", False)),
        animated=bool(obj.get("animated", False)),
    )


def _parse_context(obj: Any) -> Context | None:
    if not isinstance(obj, dict):
        return None
    return Context(
        quoted_message_id=_str(obj.get("id")),
        quoted_from=_str(obj.get("from")),
        forwarded=bool(obj.get("forwarded", False)),
        frequently_forwarded=bool(obj.get("frequently_forwarded", False)),
        referred_product=obj.get("referred_product")
        if isinstance(obj.get("referred_product"), dict)
        else None,
    )


def _parse_referral(obj: Any) -> Referral | None:
    if not isinstance(obj, dict):
        return None
    return Referral(
        source_url=_str(obj.get("source_url")),
        source_type=_str(obj.get("source_type")),
        source_id=_str(obj.get("source_id")),
        headline=_str(obj.get("headline")),
        body=_str(obj.get("body")),
        media_type=_str(obj.get("media_type")),
        ctwa_clid=_str(obj.get("ctwa_clid")),
    )


def _parse_message(
    msg: dict[str, Any],
    contacts_by_wa_id: dict[str, dict[str, Any]],
    contacts_by_user_id: dict[str, dict[str, Any]],
    single_contact: dict[str, Any] | None,
    metadata: dict[str, Any],
) -> InboundMessage | None:
    wamid = msg.get("id")
    if not wamid:
        return None
    from_phone = _str(msg.get("from"))
    from_bsuid = _str(msg.get("from_user_id"))
    contact = None
    if from_phone and from_phone in contacts_by_wa_id:
        contact = contacts_by_wa_id[from_phone]
    elif from_bsuid and from_bsuid in contacts_by_user_id:
        contact = contacts_by_user_id[from_bsuid]
    elif single_contact is not None:
        contact = single_contact
    profile = (contact or {}).get("profile") if isinstance((contact or {}).get("profile"), dict) else {}
    if not from_phone and contact and contact.get("wa_id"):
        from_phone = _str(contact["wa_id"])
    if not from_bsuid and contact and contact.get("user_id"):
        from_bsuid = _str(contact["user_id"])

    mtype = _str(msg.get("type")) or "unknown"
    message = InboundMessage(
        wamid=str(wamid),
        type=mtype,
        timestamp=_int(msg.get("timestamp"), 0),
        business_phone_number_id=_str(metadata.get("phone_number_id")),
        business_display_number=_str(metadata.get("display_phone_number")),
        from_phone=from_phone,
        from_bsuid=from_bsuid,
        display_name=_str(profile.get("name")) if profile else None,
        username=_str(profile.get("username")) if profile else None,
        context=_parse_context(msg.get("context")),
        referral=_parse_referral(msg.get("referral")),
        errors=[_parse_error(e) for e in (msg.get("errors") or [])],
        raw=msg,
    )

    body = msg.get(mtype) if isinstance(msg.get(mtype), dict) else {}
    if mtype == "text":
        message.text = _str(body.get("body"))
    elif mtype in MEDIA_TYPES:
        message.media = _parse_media(mtype, body)
    elif mtype == "location":
        message.location = Location(
            latitude=_float(body.get("latitude")),
            longitude=_float(body.get("longitude")),
            name=_str(body.get("name")),
            address=_str(body.get("address")),
            url=_str(body.get("url")),
        )
    elif mtype == "contacts":
        cards = msg.get("contacts") if isinstance(msg.get("contacts"), list) else []
        message.contacts = [_parse_contact_card(c) for c in cards if isinstance(c, dict)]
    elif mtype == "interactive":
        itype = _str(body.get("type"))
        inner = body.get(itype) if itype and isinstance(body.get(itype), dict) else {}
        message.interactive = {"type": itype, **inner}
    elif mtype == "button":
        message.button = {"payload": _str(body.get("payload")), "text": _str(body.get("text"))}
    elif mtype == "reaction":
        message.reaction = {"message_id": _str(body.get("message_id")), "emoji": _str(body.get("emoji"))}
    elif mtype == "order":
        message.order = {
            "catalog_id": _str(body.get("catalog_id")),
            "text": _str(body.get("text")),
            "product_items": [p for p in (body.get("product_items") or []) if isinstance(p, dict)],
        }
    elif mtype == "system":
        message.system = dict(body)
    return message


# ----------------------------------------------------------------------------- entry point
def parse_webhook(payload: Any, phone_number_id_filter: str | None = None) -> ParsedWebhook:
    """Parse one webhook POST body. Never raises on malformed input; unknown shapes yield no messages."""
    result = ParsedWebhook()
    if not isinstance(payload, dict):
        return result
    result.object_type = _str(payload.get("object"))
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field_name = _str(change.get("field")) or "?"
            value = change.get("value")
            if field_name != "messages" or not isinstance(value, dict):
                result.other_fields.append(field_name)
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            if phone_number_id_filter and _str(metadata.get("phone_number_id")) != phone_number_id_filter:
                result.skipped_messages += len(value.get("messages") or [])
                continue
            result.statuses += len(value.get("statuses") or [])
            result.value_errors.extend(_parse_error(e) for e in (value.get("errors") or []))
            contacts = [c for c in (value.get("contacts") or []) if isinstance(c, dict)]
            by_wa_id = {str(c["wa_id"]): c for c in contacts if c.get("wa_id")}
            by_user_id = {str(c["user_id"]): c for c in contacts if c.get("user_id")}
            single = contacts[0] if len(contacts) == 1 else None
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                parsed = _parse_message(msg, by_wa_id, by_user_id, single, metadata)
                if parsed is not None:
                    result.messages.append(parsed)
    return result
