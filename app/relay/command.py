"""Read an employee's "WhatsApp update" email: the subject line and the Name / Message block.

The format, as agreed with the owner:

    Subject:  WA | <phone number> | <booking number>      (booking number optional)

    Name:<customer name>
    Message:
    <the update, one or more lines>
    ##
    <signature, disclaimers ... ignored>

Everything here is pure text handling, so it can be tested without any mailbox or WhatsApp account.
A command that breaks a rule raises :class:`CommandRefused`; its ``text`` is shown to the employee in the
"NOT sent" email, so it says in plain English what was wrong and how to fix it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from app.relay.html import unwrap_safelink

# "WA" (any case), optional spaces, then the first "|". Anything in front (RE:, FW:, a tag) means it is not a
# command: replying to or forwarding an earlier command or confirmation must never send anything again.
_COMMAND_START = re.compile(r"WA\s*\|", re.IGNORECASE)
# The accepted rule: a line starting with "Name:" / "Message:" in any letter case (no space before the colon).
_LABEL_NAME = re.compile(r"name:", re.IGNORECASE)
_LABEL_MESSAGE = re.compile(r"message:", re.IGNORECASE)
_END = "##"
# spaces that Outlook and Exchange put into text: no-break, thin, figure, ideographic and the like
_ODD_SPACES = re.compile(
    "[" + "".join(chr(c) for c in (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)) + "]"
)
# characters that are invisible in an email but would reach the customer: zero-width space, word joiner and
# byte-order mark. Zero-width (non-)joiners stay: emoji sequences and several scripts need them.
_INVISIBLE = re.compile("[" + "".join(chr(c) for c in (0x200B, 0x2060, 0xFEFF)) + "]")
# A bare Safe Links address in the text, up to the next space. Punctuation at its end (")", ".", ",") is not
# part of it: _unwrap_links splits it off. Matched greedily and split with str.rstrip, both in linear time; a
# lazy match with a look-ahead for that punctuation took quadratic time on a long run of it, and parsing
# runs on the event loop, so one such email stopped the webhook and the forwarder for minutes or hours.
_SAFELINK = re.compile(r"https://[A-Za-z0-9.-]+\.safelinks\.protection\.outlook\.com/\S+", re.IGNORECASE)
_LINK_END_PUNCTUATION = ")].,;:!?'\""
# What may stand between the digits of a phone number: spaces, dashes of any kind, dots and brackets.
_PHONE_PUNCTUATION = re.compile(
    r"[\s.()\[\]\-" + "".join(chr(c) for c in (*range(0x2010, 0x2016), 0x2212, 0xFE63, 0xFF0D)) + "]"
)


class CommandRefused(Exception):
    """The email breaks a rule of the format; nothing is sent and its author is told ``text``."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass(frozen=True, slots=True)
class SubjectParts:
    phone_raw: str
    booking: str | None


@dataclass(frozen=True, slots=True)
class BodyParts:
    name: str  # as it goes to the customer (the fallback word when the Name line was empty)
    message: str  # flattened into one line
    name_was_empty: bool
    lines_joined: bool  # the employee's Message had line breaks that were joined with spaces


def looks_like_command(subject: str | None) -> bool:
    """Whether the subject starts with ``WA |`` (any case, any spaces). RE:/FW: in front -> False."""
    return bool(_COMMAND_START.match((subject or "").strip()))


def parse_subject(subject: str) -> SubjectParts:
    """Split ``WA | phone | booking``. Call only when :func:`looks_like_command` is true."""
    fields = [field.strip() for field in (subject or "").strip().split("|")]
    # fields[0] is "WA"
    if len(fields) > 3:
        raise CommandRefused(
            "subject_fields",
            "The subject has too many | separators. Write it as: WA | phone number | booking number"
            " (the booking number may be left out).",
        )
    phone = fields[1] if len(fields) > 1 else ""
    booking = fields[2] if len(fields) > 2 else ""
    if not phone:
        raise CommandRefused(
            "phone_missing",
            "The subject has no phone number. Write it as: WA | phone number | booking number.",
        )
    return SubjectParts(phone_raw=phone, booking=booking or None)


def normalize_target_phone(raw: str, default_region: str = "US") -> str:
    """The customer's number in E.164 form (``+16505551234``).

    10 digits, or 11 digits starting with 1, are numbers of ``default_region`` (US): spaces, dashes,
    dots, brackets and slashes do not matter, and a leading ``+1`` is fine. Any other number must be
    written with ``+`` and its country code. ``00`` and ``011`` prefixes are refused rather than guessed.
    """
    text = _INVISIBLE.sub("", _ODD_SPACES.sub(" ", raw or "")).strip()
    compact = _PHONE_PUNCTUATION.sub("", text)
    if not compact:
        raise CommandRefused("phone_missing", "The subject has no phone number.")
    plus = compact.startswith("+")
    digits = compact[1:] if plus else compact
    if not digits.isdigit():
        raise CommandRefused(
            "phone_invalid",
            f"The phone number {raw.strip()!r} holds characters other than digits (an extension or letters?)."
            " Write only the number, for example 650-555-1234 or +44 20 7946 0958.",
        )
    if not plus and (digits.startswith("00") or digits.startswith("011")):
        raise CommandRefused(
            "phone_prefix",
            f"The phone number {raw.strip()!r} starts with an international dialling prefix. Write it with +"
            " and the country code instead, for example +44 20 7946 0958.",
        )
    if plus:
        candidate, region = "+" + digits, None
    elif len(digits) == 10 or (len(digits) == 11 and digits.startswith("1")):
        candidate, region = digits, default_region
    else:
        raise CommandRefused(
            "phone_invalid",
            f"The phone number {raw.strip()!r} is not a 10-digit US number. Numbers in other countries"
            " must be written with + and the country code, for example +44 20 7946 0958.",
        )
    try:
        parsed = phonenumbers.parse(candidate, region)
    except NumberParseException as exc:
        raise CommandRefused("phone_invalid", f"The phone number {raw.strip()!r} is not valid.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise CommandRefused(
            "phone_invalid",
            f"The phone number {raw.strip()!r} is not a valid phone number. Check it for a typing mistake.",
        )
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def flatten(text: str) -> str:
    """One line, as WhatsApp accepts in a template variable: line breaks and tabs become single spaces."""
    text = _INVISIBLE.sub("", _ODD_SPACES.sub(" ", text or ""))
    return " ".join(text.split())


def _unwrap_links(text: str) -> str:
    def unwrap(match: re.Match[str]) -> str:
        found = match.group(0)
        # the address ends before the punctuation at its end, but keeps at least one character after its "/"
        shortest = found.index("/", len("https://")) + 2
        end = max(shortest, len(found.rstrip(_LINK_END_PUNCTUATION)))
        return unwrap_safelink(found[:end]) + found[end:]

    return _SAFELINK.sub(unwrap, text)


def _lines(text: str) -> list[str]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [_INVISIBLE.sub("", _ODD_SPACES.sub(" ", line)) for line in text.split("\n")]


def parse_body(
    text: str, *, max_name: int = 60, max_message: int = 820, fallback_name: str = "there"
) -> BodyParts:
    """Find ``Name:``, ``Message:`` and the closing ``##`` in the email text (see the module docstring)."""
    lines = _lines(text)
    name_at = next((i for i, line in enumerate(lines) if _LABEL_NAME.match(line.strip())), None)
    if name_at is None:
        raise CommandRefused(
            "name_missing",
            'The email has no "Name:" line. Start the text with Name:, then Message:, then the update, then a'
            " line with only ## on it.",
        )
    first = lines[name_at].strip()
    name = first[_LABEL_NAME.match(first).end() :].strip()
    message_search_from = name_at + 1
    if not name:
        # the name may stand on the next line, unless that line is already the Message: line
        for j in range(name_at + 1, len(lines)):
            candidate = lines[j].strip()
            if not candidate:
                continue
            if not _LABEL_MESSAGE.match(candidate):
                name = candidate
                message_search_from = j + 1
            break
    message_at = next(
        (i for i in range(message_search_from, len(lines)) if _LABEL_MESSAGE.match(lines[i].strip())), None
    )
    if message_at is None:
        raise CommandRefused(
            "message_missing",
            'The email has no "Message:" line after the Name: line. Write Message: on its own line, the'
            " update below it, and a line with only ## at the end.",
        )
    end_at = next((i for i in range(message_at + 1, len(lines)) if lines[i].strip() == _END), None)
    head = lines[message_at].strip()
    same_line = head[_LABEL_MESSAGE.match(head).end() :]
    if end_at is None:
        raise CommandRefused(
            "end_missing",
            "The email has no line with only ## on it, so the end of the message is unknown (the signature"
            " below it must never reach the customer). Put ## on its own line right after the message.",
        )
    body_lines = [same_line, *lines[message_at + 1 : end_at]]
    raw_message = "\n".join(body_lines).strip()
    message = flatten(_unwrap_links(raw_message))
    if not message:
        raise CommandRefused("message_empty", "The message between Message: and ## is empty.")
    if len(message) > max_message:
        raise CommandRefused(
            "message_too_long",
            f"The message is {len(message):,} characters long; at most {max_message:,} are allowed."
            " Shorten it and send it again.",
        )
    name = flatten(_unwrap_links(name))
    if len(name) > max_name:
        raise CommandRefused(
            "name_too_long",
            f"The name is {len(name)} characters long; at most {max_name} are allowed (it is the greeting:"
            ' "Hello <name>,").',
        )
    lines_joined = sum(1 for line in body_lines if line.strip()) > 1
    if not name:
        return BodyParts(name=fallback_name, message=message, name_was_empty=True, lines_joined=lines_joined)
    return BodyParts(name=name, message=message, name_was_empty=False, lines_joined=lines_joined)
