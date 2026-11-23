"""What the relay reads from an email in its mailbox before it treats it as a command.

An email only counts as a command when a person on the allow-list wrote it and really sent it
from their own mailbox. :class:`MailInfo` holds the parts of a Graph message that decide this:

* who wrote it (From) and who really sent it (Sender). They must be the same person: an email
  sent on behalf of someone else is not a command;
* Exchange's mark for how the sender signed in (``X-MS-Exchange-Organization-AuthAs``), see
  :func:`internal_auth_ok`;
* the signs of automatic mail (out-of-office replies, bounces, read receipts, Power Automate flows,
  list mail), see :func:`automatic_mail`. Such mail is never a command;
* whether its subject is still the Subject line Exchange delivered it with, see :func:`subject_edited`,
  and whether it was delivered at all: a draft was written in the mailbox by someone with write access
  to it, with whatever From line and headers they chose.

The Name / Message text itself is read by :mod:`app.relay.command`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any

# Defined once, next to the Graph code that needs it too; importable from here for the mail checks.
from app.relay.mailbox import valid_address

AUTH_AS_HEADER = "x-ms-exchange-organization-authas"
_SUBJECT_PREFIX = re.compile(r"^(?:\s*[^\W\d_]{1,6}(?:\[\d+\])?\s*[:\uff1a])+")  # RE:, FW:, AW:, RE[2]: ...
_AUTOMATIC_HEADERS = (
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
    "x-ms-exchange-inbox-rules-loop",
    "x-ms-exchange-generated-message-source",
    "x-failed-recipients",
    "list-id",
    "list-unsubscribe",
    # Power Automate and Logic Apps: an automation, not a person, sent it
    "x-ms-mail-application",
    "x-ms-mail-operation-type",
)
_AUTOMATIC_SUBJECTS = ("automatic reply:", "autoreply:", "auto:", "out of office", "undeliverable:", "read:")


def bare_subject(subject: str) -> str:
    """A subject without "RE:", "FW:" and the like in front, in lower case, for comparing."""
    return " ".join(_SUBJECT_PREFIX.sub("", subject).split()).lower()


@dataclass(slots=True)
class MailInfo:
    """The parts of a Graph message the relay looks at."""

    author: str | None  # address in From (lower case), None if it is not a plain address
    author_name: str
    actual_sender: str | None  # who really sent it, if that can be read (a delegate's address)
    subject: str
    internet_message_id: str
    recipients: list[str]
    body: str
    body_is_html: bool
    message_class: str | None
    headers: dict[str, list[str]] = field(default_factory=dict)
    received_at: float | None = None  # Unix time the email arrived in the mailbox
    on_behalf: bool = False  # sent by someone other than the author, or by someone who cannot be named
    is_draft: bool = False  # written in the mailbox (Graph's isDraft), never delivered to it

    @classmethod
    def from_graph(cls, raw: dict[str, Any]) -> MailInfo:
        def address_of(value: Any) -> dict[str, Any]:
            inner = value.get("emailAddress") if isinstance(value, dict) else None
            return inner if isinstance(inner, dict) else {}

        author = address_of(raw.get("from"))
        headers: dict[str, list[str]] = {}
        for header in raw.get("internetMessageHeaders") or []:
            if isinstance(header, dict) and header.get("name"):
                headers.setdefault(str(header["name"]).lower(), []).append(str(header.get("value") or ""))
        message_class = None
        for prop in raw.get("singleValueExtendedProperties") or []:
            if isinstance(prop, dict) and str(prop.get("id") or "").lower().replace(" ", "") in (
                "string0x1a",
                "string0x001a",
            ):
                message_class = str(prop.get("value") or "") or None
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        recipients = [
            str(address_of(r).get("address") or "")
            for name in ("toRecipients", "ccRecipients")
            for r in (raw.get(name) if isinstance(raw.get(name), list) else [])
        ]
        # Exchange can name a person by an internal directory name instead of an address. The
        # email's own From and Sender lines then say who it is.
        from_raw = str(author.get("address") or "").strip()
        sender_raw = str(address_of(raw.get("sender")).get("address") or "").strip()
        author_address = valid_address(from_raw) or _header_address(headers.get("from"))
        sender_address = valid_address(sender_raw)
        if sender_address is None and sender_raw:
            sender_address = _header_address(headers.get("sender"))
            if sender_address is None and "sender" not in headers and sender_raw.lower() == from_raw.lower():
                sender_address = author_address  # the same unreadable name in both places: one person
        return cls(
            author=author_address,
            author_name=str(author.get("name") or "").strip(),
            actual_sender=sender_address,
            on_behalf=bool(sender_raw) and sender_address != author_address,
            subject=str(raw.get("subject") or ""),
            internet_message_id=str(raw.get("internetMessageId") or ""),
            recipients=[r for r in recipients if r],
            body=str(body.get("content") or ""),
            body_is_html=str(body.get("contentType") or "").lower() == "html",
            message_class=message_class,
            headers=headers,
            received_at=_timestamp(raw.get("receivedDateTime")),
            is_draft=raw.get("isDraft") is True,
        )

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None

    def addressed(self) -> list[str]:
        """The addresses in the To and Cc boxes (lower case), as Graph lists them and as the email's own
        To and Cc lines do. Display names are left out: anyone can type an address into a name."""
        found = [recipient.strip().lower() for recipient in self.recipients]
        for name in ("to", "cc"):
            found.extend(address.strip().lower() for _, address in getaddresses(self.headers.get(name, [])))
        return [address for address in dict.fromkeys(found) if address]


def _header_address(values: list[str] | None) -> str | None:
    """The one address a header such as From names; None if it names none, or several."""
    addresses = [address for _, address in getaddresses(values or []) if address]
    return valid_address(addresses[0]) if len(addresses) == 1 else None


def _timestamp(value: Any) -> float | None:
    """Unix time of a Graph date-time such as ``2026-11-18T15:00:00Z``; None if it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def automatic_mail(mail: MailInfo) -> str | None:
    """Why ``mail`` is not a person's command (out-of-office reply, receipt, bounce, list mail), or None."""
    message_class = (mail.message_class or "").lower()
    if message_class and message_class != "ipm.note" and not message_class.startswith("ipm.note.smime"):
        return f"not an ordinary email ({mail.message_class})"
    auto = mail.header("auto-submitted")
    if auto is not None and auto.strip().lower() != "no":
        return "an automatic message (Auto-Submitted)"
    for name in _AUTOMATIC_HEADERS:
        if name in mail.headers:
            return f"an automatic message ({name})"
    if (mail.header("precedence") or "").strip().lower() in ("bulk", "junk", "list", "auto_reply"):
        return "an automatic message (Precedence)"
    local = (mail.author or "").split("@", 1)[0]
    if local in ("mailer-daemon", "postmaster") or local.startswith("microsoftexchange"):
        return "a message from the mail system"
    if mail.subject.strip().lower().startswith(_AUTOMATIC_SUBJECTS):
        return "an automatic message (subject)"
    return None


def auth_marks(mail: MailInfo) -> list[str]:
    """Every copy of Exchange's AuthAs mark on the email, trimmed and in lower case (often just one)."""
    return [value.strip().lower() for value in mail.headers.get(AUTH_AS_HEADER, [])]


def internal_auth_ok(mail: MailInfo, required: bool) -> bool:
    """Whether Exchange's AuthAs mark allows taking the email as sent by a signed-in internal user.

    Exchange stamps how the sender signed in (``Internal`` for a user of the organisation). A sender
    can type a line that looks like the mark as well, so every copy has to say Internal: one copy
    that says anything else (``Anonymous``, ``Partner``, an empty value) is enough to distrust the
    email. With ``required`` (RELAY_REQUIRE_INTERNAL_AUTH) an email without the mark is not
    accepted either.
    """
    marks = auth_marks(mail)
    if any(mark != "internal" for mark in marks):
        return False
    return bool(marks) or not required


def subject_edited(mail: MailInfo) -> bool:
    """Whether the email's subject differs from the Subject line Exchange delivered it with.

    Exchange keeps an email's delivered internet headers (the AuthAs mark among them) with it. Editing a
    received email, which classic Outlook lets anyone with write access to the mailbox do, changes its
    subject but not those headers: an employee's ordinary email made into a command that way would pass
    every check that reads the headers. Without a Subject line there is nothing to compare; the AuthAs rule,
    which needs the headers, decides then. Line folding, encoded words, spaces and letter case are ignored.
    """
    delivered = mail.headers.get("subject") or []
    if not delivered:
        return False
    current = _comparable(mail.subject)
    return all(_comparable(_decoded_header(value)) != current for value in delivered)


def _decoded_header(value: str) -> str:
    """A header's text as a mail program shows it: unfolded, with RFC 2047 encoded words decoded."""
    unfolded = re.sub(r"\r?\n(?=[ \t])", "", value)
    try:
        return str(make_header(decode_header(unfolded)))
    except Exception:  # an encoded word that cannot be decoded is compared as it is written
        return unfolded


def _comparable(text: str) -> str:
    """A subject without any spaces (a long Subject line may be folded where it had one), in lower case."""
    return "".join(unicodedata.normalize("NFC", text).split()).casefold()
