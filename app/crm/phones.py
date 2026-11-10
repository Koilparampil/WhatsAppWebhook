"""Phone-number normalisation and CRM record matching.

CRM phone fields are free text ("(650) 555-1234", "+1 650.555.1234 x12", "650-555-1234 / 650-555-9999").
WhatsApp gives E.164 digits without the plus sign ("16505551234"). Both sides are reduced to a
:class:`PhoneKey` so they can be compared exactly (E.164) or, as a fallback, by the last ten digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

_EXTENSION_RE = re.compile(r"(?i)[\s,]*(?:ext\.?|extension|x|#)\s*\.?\s*\d{1,6}\s*$")
_SPLIT_RE = re.compile(r"\s*(?:/|;|\||,|\bor\b|\n)\s*", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_EMAIL_KEY_RE = re.compile(r"^email\d*$", re.IGNORECASE)
SUFFIX_LEN = 10


@dataclass(frozen=True, slots=True)
class PhoneKey:
    e164: str | None
    digits: str

    @property
    def suffix(self) -> str | None:
        return self.digits[-SUFFIX_LEN:] if len(self.digits) >= SUFFIX_LEN else None


def normalize_phone(raw: str | None, default_region: str | None) -> PhoneKey | None:
    """Normalise a single phone string. Returns None when it does not look like a phone number."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = _EXTENSION_RE.sub("", text)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return None
    candidate = text
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:]
    e164: str | None = None
    try:
        parsed = phonenumbers.parse(candidate, None if candidate.lstrip().startswith("+") else default_region)
        if phonenumbers.is_valid_number(parsed):
            e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    except NumberParseException:
        e164 = None
    return PhoneKey(e164=e164, digits=digits)


def normalize_phones(raw: str | None, default_region: str | None) -> list[PhoneKey]:
    """Normalise a CRM field that may hold several numbers separated by / ; , | or 'or'."""
    if raw is None or not str(raw).strip():
        return []
    keys: list[PhoneKey] = []
    for part in _SPLIT_RE.split(str(raw)):
        key = normalize_phone(part, default_region)
        if key and key not in keys:
            keys.append(key)
    return keys


def whatsapp_phone_key(from_phone: str | None) -> PhoneKey | None:
    """WhatsApp ``from`` / ``wa_id`` values are international digits without '+'."""
    if not from_phone:
        return None
    digits = re.sub(r"\D", "", from_phone)
    if len(digits) < 7:
        return None
    return normalize_phone("+" + digits, None) or PhoneKey(e164=None, digits=digits)


def extract_emails(record: dict[str, Any]) -> list[str]:
    """Collect valid, de-duplicated email addresses from every ``email*`` field, in field order."""
    keys = sorted((k for k in record if _EMAIL_KEY_RE.match(k)), key=_email_key_order)
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        value = record.get(key)
        if not value or not isinstance(value, str):
            continue
        for part in re.split(r"[;,\s]+", value.strip()):
            addr = part.strip().strip("<>")
            if addr and _EMAIL_RE.match(addr) and addr.lower() not in seen:
                seen.add(addr.lower())
                out.append(addr)
    return out


def _email_key_order(key: str) -> tuple[int, str]:
    match = re.search(r"\d+$", key)
    return (int(match.group()) if match else 0, key.lower())


# ----------------------------------------------------------------------------- CRM records
PHONE_FIELD_PRIORITY = ("mobile", "businessPhone", "phone", "deskNumber", "fax")
_ID_KEYS = (
    "id",
    "customerId",
    "shipperId",
    "consigneeId",
    "agentId",
    "exporterId",
    "truckerId",
    "notifyingPartyId",
    "userId",
)


@dataclass(slots=True)
class CrmRecord:
    entity: str
    record_id: str
    company: str | None
    contact: str | None
    active: bool
    updated_at: datetime | None
    emails: list[str]
    phones: list[tuple[str, str, PhoneKey]] = field(default_factory=list)  # (field name, raw value, key)

    @property
    def label(self) -> str:
        parts = [p for p in (self.company, self.contact) if p]
        name = " / ".join(parts) if parts else "(unnamed record)"
        return f"{self.entity} #{self.record_id}: {name}"


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an API timestamp; unset .NET dates (year 1) and other junk become None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year < 1970:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def record_from_api(entity: str, obj: dict[str, Any], default_region: str | None) -> CrmRecord | None:
    """Turn one list-endpoint object into a :class:`CrmRecord`. Returns None when it has no usable phone."""
    record_id = next((str(obj[k]) for k in _ID_KEYS if obj.get(k) is not None), None)
    if record_id is None:
        record_id = next((str(v) for k, v in obj.items() if k.lower().endswith("id") and v is not None), "?")
    company = obj.get("companyName") or obj.get("name")
    contact = obj.get("contactPerson") or obj.get("person")
    if not contact and (obj.get("firstName") or obj.get("lastName")):
        contact = " ".join(p for p in (obj.get("firstName"), obj.get("lastName")) if p)
    active_value = obj.get("active", obj.get("isActive", True))
    active = bool(active_value) if active_value is not None else True
    updated = _parse_datetime(
        obj.get("updatedDate") or obj.get("updatedAt") or obj.get("creationDate") or obj.get("createdAt")
    )
    emails = extract_emails(obj)
    if not emails and isinstance(obj.get("userName"), str) and _EMAIL_RE.match(obj["userName"].strip()):
        emails = [obj["userName"].strip()]
    phones: list[tuple[str, str, PhoneKey]] = []
    for field_name in PHONE_FIELD_PRIORITY:
        raw = obj.get(field_name)
        if isinstance(raw, str) and raw.strip():
            for key in normalize_phones(raw, default_region):
                phones.append((field_name, raw.strip(), key))
    if not phones:
        return None
    return CrmRecord(
        entity=entity,
        record_id=record_id,
        company=str(company).strip() if company else None,
        contact=str(contact).strip() if contact else None,
        active=active,
        updated_at=updated,
        emails=emails,
        phones=phones,
    )


@dataclass(slots=True)
class MatchCandidate:
    record: CrmRecord
    matched_field: str
    matched_raw: str
    exact: bool


def rank_candidates(candidates: list[MatchCandidate], entity_order: list[str]) -> list[MatchCandidate]:
    """Best match first: exact E.164 > suffix; mobile > business phone; active > inactive; newest."""

    def field_rank(name: str) -> int:
        try:
            return PHONE_FIELD_PRIORITY.index(name)
        except ValueError:
            return len(PHONE_FIELD_PRIORITY)

    def entity_rank(name: str) -> int:
        try:
            return entity_order.index(name)
        except ValueError:
            return len(entity_order)

    def sort_key(c: MatchCandidate) -> tuple[int, int, int, int, float, str]:
        try:
            updated = c.record.updated_at.timestamp() if c.record.updated_at else 0.0
        except (ValueError, OverflowError, OSError):
            updated = 0.0
        return (
            0 if c.exact else 1,
            entity_rank(c.record.entity),
            field_rank(c.matched_field),
            0 if c.record.active else 1,
            -updated,
            c.record.record_id,
        )

    # one candidate per record (keep its best field)
    best_per_record: dict[tuple[str, str], MatchCandidate] = {}
    for cand in sorted(candidates, key=sort_key):
        key = (cand.record.entity, cand.record.record_id)
        best_per_record.setdefault(key, cand)
    return sorted(best_per_record.values(), key=sort_key)
