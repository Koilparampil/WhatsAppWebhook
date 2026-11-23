"""Every email the "WhatsApp updates" feature sends: outcome notices to the employee and alerts to ALERT_TO.

An employee who emails a ``WA | phone | booking`` command always hears back (owner decision 4): the update
was sent, was NOT sent (with the reason and the exact format to use), MAY NOT have been sent, would have
been sent (dry run), or was sent but WhatsApp later reported that it could not deliver it. Administrators
get the feature's alerts.

The builders are pure: they take what the processor decided and return an :class:`EmailMessage` (policy
SMTP, text and HTML alternatives drawn by the forwarder's own ``_render_text``/``_render_html`` so every
email looks alike). They never touch the store, the mailbox or WhatsApp. Rules every email follows, and why:

* ``Auto-Submitted: auto-generated`` and ``X-Auto-Response-Suppress: All``: out-of-office replies must not
  come back to the relay mailbox, and mail marked like this is never read as a command.
* ``In-Reply-To``/``References`` name the employee's command, so the outcome lands in the same conversation.
* No subject starts with "WA" (any case): an email of ours that reaches the relay mailbox must never look
  like a command.
* Nothing secret or internal goes in: no access tokens, CRM credentials, Graph or WhatsApp ids. Callers
  must not pass such ids; configured secret values are also removed from any text a caller passes in, as a
  last line of defence.
* Phone numbers are shown in full: the employee typed them (logs always mask them, app.worker.mask_phone).

A notice is queued as a KIND_NOTE item and sent later by the worker, so :func:`to_payload` turns it into
JSON-safe fields and :func:`from_payload` rebuilds the identical email (same headers, text and HTML).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate
from typing import Any, NamedTuple

from app.config import Settings
from app.mail.compose import (
    _clean_header_text,
    _local_time,
    _render_html,
    _render_text,
    format_phone_for_humans,
)
from app.whatsapp.send import explain_error

FROM_NAME = "WhatsApp updates"
RELAY_HEADER = "X-WhatsApp-Relay"  # its value is the notice kind below, for mail rules and tests

KIND_SENT = "sent"
KIND_REFUSED = "refused"
KIND_UNCERTAIN = "uncertain"
KIND_DRY_RUN = "dry_run"
KIND_DELIVERY_FAILED = "delivery_failed"
KIND_ALERT = "alert"


class FileResult(NamedTuple):
    """What happened to one attached file.

    ``sent`` is True (sent), False (not sent) or None (it may or may not have been sent: WhatsApp gave no
    clear answer). ``reason`` says why in plain English; for a sent file it may say how it was sent.
    A plain ``(filename, sent, reason)`` tuple is accepted wherever a FileResult is.
    """

    filename: str
    sent: bool | None
    reason: str | None = None


FileEntry = FileResult | tuple[str, bool | None, str | None]

# The refusal codes of app.relay.command (a mistake in the email itself) ...
_FORMAT_REFUSALS = (
    "subject_fields",
    "phone_missing",
    "phone_invalid",
    "phone_prefix",
    "name_missing",
    "name_too_long",
    "message_missing",
    "end_missing",
    "message_empty",
    "message_too_long",
)
# ... and the processor's own. The subject of a NOT-sent notice says in a few words what went wrong.
_SHORT_REASONS = {
    "subject_fields": "too many | in the subject",
    "phone_missing": "no phone number in the subject",
    "phone_invalid": "the phone number is not valid",
    "phone_prefix": "write the number with + and the country code",
    "name_missing": 'no "Name:" line',
    "name_too_long": "the name is too long",
    "message_missing": 'no "Message:" line',
    "end_missing": "no line with only ## after the message",
    "message_empty": "the message is empty",
    "message_too_long": "the message is too long",
    "too_old": "your email is too old",
    "test_mode": "test mode is on (not a test number)",
    "template_not_ready": "the WhatsApp template is not ready",
}
_FIX_AND_RESEND = (
    "Correct your email and send it again as a new email, not as a reply (the format is shown below)."
)
_NEXT_STEPS = {
    **dict.fromkeys(_FORMAT_REFUSALS, _FIX_AND_RESEND),
    "too_old": "If the update is still needed, send it again as a new email.",
    "test_mode": (
        "While test mode is on, updates can only go to the test numbers. Contact the customer another way,"
        " or ask the administrator."
    ),
    "template_not_ready": (
        "Nothing can be sent until the WhatsApp template is ready again; an administrator has been alerted."
        " Contact the customer another way if the update cannot wait, or send it again later as a new email."
    ),
}
_DEFAULT_NEXT_STEP = (
    "If the reason above can be fixed (for example a wrong phone number), correct it and send the update"
    " again as a new email. Otherwise contact the customer another way, for example by phone or email."
)
_SHORT_REASON_MAX = 80
_ANSWERS_FORWARDED = (
    "If the customer answers on WhatsApp, the answer arrives by email in the team's WhatsApp inbox, like"
    " every other WhatsApp message to VShip; it does not come to you personally."
)
_UNCERTAIN_DEFAULT = "WhatsApp did not confirm that it took the message, so it may or may not have been sent."

# Settings that hold secrets. Any of their values found in caller text is replaced, so a notice can never
# carry one even if an error message quoted it. Very short values are skipped: replacing them would
# mangle ordinary words, and they are not real secrets anyway.
_SECRET_SETTINGS = (
    "wa_access_token",
    "wa_app_secret",
    "wa_verify_token",
    "crm_password",
    "smtp_password",
    "oauth_client_secret",
    "onboard_key",
)
_MIN_SECRET_LENGTH = 8
_REMOVED = "[removed]"

_ADDRESS = re.compile(r"[^@\s<>\",;:\\()\[\]]+@[^@\s<>\",;:\\()\[\]]+")
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>|([^<>\s]+)")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps tab and line feed, handled separately
_HEADER_NAME = re.compile(r"[!-9;-~]+")  # RFC 5322 field name: printable ASCII except ":"


# ----------------------------------------------------------------------------- notices to the employee
def build_sent_notice(
    settings: Settings,
    *,
    employee_address: str,
    employee_name: str | None = None,
    command_message_id: str | None = None,
    command_subject: str | None = None,
    phone: str,
    booking: str | None = None,
    crm_match: str | None = None,
    template_name: str,
    template_language: str,
    customer_text: str | None = None,
    parameters: Sequence[str] = (),
    files: Sequence[FileEntry] = (),
    notes: Sequence[str] = (),
    name_fallback_used: bool = False,
    lines_joined: bool = False,
    test_mode: bool = False,
    sent_at: float | None = None,
    now: float | None = None,
) -> EmailMessage:
    """WhatsApp accepted the update (the template step is ``sent``); files may or may not have followed."""
    number = _number(phone)
    booking = _clean_header_text(booking) or None
    results = _file_results(files)
    not_sent = sum(1 for result in results if result.sent is False)
    unsure = sum(1 for result in results if result.sent is None)
    happened = [
        f"WhatsApp accepted your update for {number}.",
        "If WhatsApp later reports that it could not deliver it, you will get another email.",
    ]
    if not_sent:
        happened.append(f"Attached files NOT sent: {not_sent} of {len(results)} (see Files below).")
    if unsure:
        happened.append(
            f"Attached files that MAY NOT have been sent: {unsure} of {len(results)} (see Files below)."
        )
    todo = [] if not_sent or unsure else ["Nothing more to do."]
    if not_sent:
        todo.append("Send the files marked NOT sent to the customer another way, for example by email.")
    if unsure:
        todo.append(
            "For the files marked MAY NOT have been sent: check the chat in the WhatsApp Business app and"
            " send any file that is missing another way, for example by email."
        )
    todo.append(_ANSWERS_FORWARDED)
    sections = [
        ("What happened", happened),
        ("What to do next", todo),
        *_details(
            number=number,
            crm_match=crm_match,
            text_title="What the customer received",
            customer_text=customer_text,
            parameters=parameters,
            template_name=template_name,
            template_language=template_language,
            files=_file_lines(results),
        ),
        (
            "Notes",
            _notes(
                notes,
                name_fallback_used=name_fallback_used,
                lines_joined=lines_joined,
                test_mode=test_mode,
            ),
        ),
    ]
    return _notice(
        settings,
        KIND_SENT,
        to=_employee(employee_address, employee_name),
        subject=f"WhatsApp update sent: {number}" + (f" (booking {booking})" if booking else ""),
        in_reply_to=command_message_id,
        header=_header_rows(
            settings,
            "Sent",
            booking=booking,
            when=sent_at,
            when_label="Sent",
            command_subject=command_subject,
        ),
        sections=sections,
        now=now,
    )


def build_dry_run_notice(
    settings: Settings,
    *,
    employee_address: str,
    employee_name: str | None = None,
    command_message_id: str | None = None,
    command_subject: str | None = None,
    phone: str,
    booking: str | None = None,
    crm_match: str | None = None,
    template_name: str,
    template_language: str,
    customer_text: str | None = None,
    parameters: Sequence[str] = (),
    files: Sequence[FileEntry] = (),
    notes: Sequence[str] = (),
    name_fallback_used: bool = False,
    lines_joined: bool = False,
    test_mode: bool = False,
    now: float | None = None,
) -> EmailMessage:
    """RELAY_DRY_RUN: every check passed, nothing went to WhatsApp; ``files`` say what WOULD be sent."""
    number = _number(phone)
    booking = _clean_header_text(booking) or None
    results = _file_results(files)
    held_back = sum(1 for result in results if result.sent is not True)
    happened = [
        "DRY RUN: this is what would have been sent. Nothing was sent to WhatsApp, and the customer received"
        " nothing.",
        "The WhatsApp update service is still being tested, so for now it only checks emails.",
    ]
    if held_back:
        happened.append(
            f"Attached files that would NOT be sent: {held_back} of {len(results)} (see Files below)."
        )
    todo = [
        "Check that the text below is what you meant to send.",
        "If the customer needs this update now, contact them another way: nothing reaches customers during"
        " the test.",
    ]
    sections = [
        ("What happened", happened),
        ("What to do next", todo),
        *_details(
            number=number,
            crm_match=crm_match,
            text_title="What the customer would receive",
            customer_text=customer_text,
            parameters=parameters,
            template_name=template_name,
            template_language=template_language,
            files=_file_lines(results, dry_run=True),
        ),
        (
            "Notes",
            _notes(
                notes,
                name_fallback_used=name_fallback_used,
                lines_joined=lines_joined,
                test_mode=test_mode,
            ),
        ),
    ]
    return _notice(
        settings,
        KIND_DRY_RUN,
        to=_employee(employee_address, employee_name),
        subject=f"DRY RUN - WhatsApp update not sent: {number}",
        in_reply_to=command_message_id,
        header=_header_rows(
            settings, "DRY RUN - nothing sent", booking=booking, command_subject=command_subject
        ),
        sections=sections,
        now=now,
    )


def build_uncertain_notice(
    settings: Settings,
    *,
    employee_address: str,
    employee_name: str | None = None,
    command_message_id: str | None = None,
    command_subject: str | None = None,
    phone: str,
    booking: str | None = None,
    crm_match: str | None = None,
    template_name: str | None = None,
    template_language: str | None = None,
    customer_text: str | None = None,
    parameters: Sequence[str] = (),
    files: Sequence[FileEntry] = (),
    notes: Sequence[str] = (),
    uncertainty: str | None = None,
    name_fallback_used: bool = False,
    lines_joined: bool = False,
    test_mode: bool = False,
    sent_at: float | None = None,
    now: float | None = None,
) -> EmailMessage:
    """The update request went out but WhatsApp gave no clear answer; it is never retried (decision 13).

    ``uncertainty`` is what went wrong in plain English (``WhatsAppSendError.explanation``); ``sent_at`` is
    when the send was tried. Files are never sent after an unclear update, so they are listed as not sent.
    """
    number = _number(phone)
    booking = _clean_header_text(booking) or None
    results = _file_results(files)
    mailbox = _mailbox(settings)
    happened = [
        *(_lines(uncertainty) or [_UNCERTAIN_DEFAULT]),
        "The service never sends the same update twice on its own, so it was not tried again.",
    ]
    todo = [
        f"Open the chat with {number} in the WhatsApp Business app.",
        "If your update is there, do nothing more: sending it again would give the customer the same update"
        " twice.",
        f"If it is not there, send it again as a new email to {mailbox}.",
    ]
    if results:
        todo.append(
            "Your attached files were not sent (see Files below). If the update is there but the files are"
            " not, send the files another way, for example by email."
        )
    sections = [
        ("What happened", happened),
        ("What to do next", todo),
        *_details(
            number=number,
            crm_match=crm_match,
            text_title="What the customer may have received",
            customer_text=customer_text,
            parameters=parameters,
            template_name=template_name,
            template_language=template_language,
            files=_file_lines(results, default_reason="the update itself may not have been sent"),
        ),
        (
            "Notes",
            _notes(
                notes,
                name_fallback_used=name_fallback_used,
                lines_joined=lines_joined,
                test_mode=test_mode,
            ),
        ),
    ]
    return _notice(
        settings,
        KIND_UNCERTAIN,
        to=_employee(employee_address, employee_name),
        subject=f"WhatsApp update MAY NOT have been sent: {number}",
        in_reply_to=command_message_id,
        header=_header_rows(
            settings,
            "MAY NOT have been sent",
            booking=booking,
            when=sent_at,
            when_label="Tried at",
            command_subject=command_subject,
        ),
        sections=sections,
        now=now,
    )


def build_refused_notice(
    settings: Settings,
    *,
    employee_address: str,
    employee_name: str | None = None,
    command_message_id: str | None = None,
    command_subject: str | None = None,
    refusal_code: str,
    refusal_text: str,
    short_reason: str | None = None,
    next_step: str | None = None,
    phone: str | None = None,
    booking: str | None = None,
    crm_match: str | None = None,
    template_name: str | None = None,
    template_language: str | None = None,
    error_code: int | None = None,
    error_explanation: str | None = None,
    files: Sequence[FileEntry] = (),
    notes: Sequence[str] = (),
    test_mode: bool = False,
    dry_run: bool = False,
    now: float | None = None,
) -> EmailMessage:
    """Nothing was sent: the email broke a rule, or WhatsApp refused the update outright.

    ``refusal_text`` is the plain-English reason (``CommandRefused.text`` or the processor's own) and
    ``refusal_code`` its stable code. The subject's short reason comes from the code; for a code this module
    does not know (a WhatsApp refusal, say), from ``short_reason`` or else the reason's first sentence.
    ``error_code``/``error_explanation`` are Meta's, when WhatsApp refused it. The email always ends with the
    exact command format.
    """
    code = (refusal_code or "").strip()
    reason = _lines(refusal_text) or ["The email could not be used."]
    happened = [
        "Your update was NOT sent. The customer received nothing.",
        f"Reason: {reason[0]}",
        *reason[1:],
    ]
    explanation = " ".join(_lines(error_explanation))
    if explanation and explanation not in " ".join(reason):
        happened.append(f"WhatsApp's explanation: {explanation}")
    if error_code is not None:
        happened.append(f"WhatsApp error code: {error_code}")
    todo = _lines(next_step) or [_NEXT_STEPS.get(code, _DEFAULT_NEXT_STEP)]
    booking = _clean_header_text(booking) or None
    number = _number(phone) if _clean_header_text(phone) else None
    to_lines = [f"Phone number: {number}"] if number else []
    if crm_match and number:
        to_lines.append(f"VShipCRM: {crm_match}")
    sections = [
        ("What happened", happened),
        ("What to do next", todo),
        ("To", to_lines),
        ("Template", _template_lines(template_name, template_language)),
        ("Files", _file_lines(_file_results(files), default_reason="the update itself was not sent")),
        ("Notes", _notes(notes, test_mode=test_mode and code != "test_mode", dry_run=dry_run)),
        ("How to write a WhatsApp update email", _format_reminder(settings)),
    ]
    summary = _clean_header_text(short_reason) or _SHORT_REASONS.get(code) or _first_sentence(reason[0])
    return _notice(
        settings,
        KIND_REFUSED,
        to=_employee(employee_address, employee_name),
        subject=f"WhatsApp update NOT sent: {summary}",
        in_reply_to=command_message_id,
        header=_header_rows(settings, "NOT sent", booking=booking, command_subject=command_subject),
        sections=sections,
        now=now,
    )


def build_delivery_failed_notice(
    settings: Settings,
    *,
    employee_address: str,
    employee_name: str | None = None,
    command_message_id: str | None = None,
    command_subject: str | None = None,
    phone: str,
    booking: str | None = None,
    filename: str | None = None,
    template_name: str | None = None,
    error_code: int | None = None,
    error_title: str | None = None,
    error_details: str | None = None,
    error_explanation: str | None = None,
    sent_at: float | None = None,
    now: float | None = None,
) -> EmailMessage:
    """WhatsApp took a message this feature sent, then reported it ``failed`` (status webhook).

    ``filename`` names the file when the failed message was a file; None means the update itself.
    Without ``error_explanation`` the explanation comes from ``explain_error(code, title, details)``.
    """
    number = _number(phone)
    booking = _clean_header_text(booking) or None
    name = _clean_header_text(filename)
    if name:
        failed, what_line = f"the file {name}", f"The file {name}"
    else:
        failed = "your update"
        what_line = "Your WhatsApp update" + (f" (template {template_name})" if template_name else "")
    explanation = " ".join(_lines(error_explanation)) or explain_error(error_code, error_title, error_details)
    happened = [
        f"WhatsApp could not deliver {failed} to {number}.",
        f"Reason: {explanation}",
        f"WhatsApp error code: {error_code}" if error_code is not None else "WhatsApp gave no error code.",
    ]
    todo = [f"The customer has not received {'this file' if name else 'this update'}."]
    if name:
        todo.append("Send the file to the customer another way, for example by email.")
    else:
        todo.append("If the phone number is wrong, send a new update email with the right number.")
        todo.append("Otherwise contact the customer another way, for example by phone or email.")
    sections = [
        ("What happened", happened),
        ("What to do next", todo),
        ("To", [f"Phone number: {number}"]),
        ("What could not be delivered", [what_line]),
    ]
    return _notice(
        settings,
        KIND_DELIVERY_FAILED,
        to=_employee(employee_address, employee_name),
        subject=f"WhatsApp could not deliver your update to {number}",
        in_reply_to=command_message_id,
        header=_header_rows(
            settings,
            "Not delivered",
            booking=booking,
            when=sent_at,
            when_label="Sent",
            command_subject=command_subject,
        ),
        sections=sections,
        now=now,
    )


# ----------------------------------------------------------------------------- alerts to ALERT_TO
def build_relay_alert(
    subject: str, lines: Sequence[str], settings: Settings, *, now: float | None = None
) -> EmailMessage:
    """An alert for the administrators (ALERT_TO), in the subject style of the forwarder's other alerts.

    Used for the not-allowed digest, a template that is not approved or not Utility, the mailbox being
    unreadable for 15 minutes (and readable again) and a dead-lettered relay item. ``lines`` should say what
    happened and what to do about it.
    """
    title = _clean_header_text(subject) or "WhatsApp updates"
    body = [line for text in lines for line in _lines(text)]
    stamp = time.time() if now is None else now
    return _notice(
        settings,
        KIND_ALERT,
        to=settings.alert_to_list,
        subject=f"{settings.mail_subject_prefix} ALERT: {title}",
        in_reply_to=None,
        header=[("Alert", title), ("Time", _local_time(int(stamp), settings.mail_timezone))],
        sections=[("Details", body or ["(no details)"])],
        now=stamp,
    )


# ----------------------------------------------------------------------------- queue payload
_NOT_IN_HEADERS = ("to", "subject", "mime-version")


def to_payload(message: EmailMessage) -> dict[str, Any]:
    """The email as JSON-safe fields for a KIND_NOTE queue item: to, subject, headers, text, html.

    ``to`` holds the recipients as written in the To header and ``headers`` every other header in order,
    except the MIME structure headers (Content-*, MIME-Version) that rebuilding recreates. Line endings are
    stored as ``\\n``; the MIME encoder splits lines the same way for either, so nothing is lost.
    """
    to_header = message["To"]
    addresses = [str(address) for address in getattr(to_header, "addresses", ())]
    if not addresses:
        raise ValueError("the email has no recipients")
    if any(True for _ in message.iter_attachments()):
        raise ValueError("a notice cannot carry attachments")
    text_part = message.get_body(("plain",))
    if text_part is None:
        raise ValueError("the email has no text part")
    html_part = message.get_body(("html",))
    return {
        "to": addresses,
        "subject": str(message["Subject"] or ""),
        "headers": [[name, str(value)] for name, value in message.items() if _kept_header(name)],
        "text": _unix_lines(text_part.get_content()),
        "html": _unix_lines(html_part.get_content()) if html_part is not None else None,
    }


def from_payload(payload: Mapping[str, Any]) -> EmailMessage:
    """Rebuild the email :func:`to_payload` stored. Raises ValueError for a payload it did not write."""
    if not isinstance(payload, Mapping):
        raise ValueError("a notice payload must be a JSON object")
    to = payload.get("to")
    subject = payload.get("subject")
    headers = payload.get("headers")
    text = payload.get("text")
    html_body = payload.get("html")
    if not isinstance(to, list | tuple) or not to or not all(isinstance(a, str) and a.strip() for a in to):
        raise ValueError("notice payload: 'to' must be a non-empty list of addresses")
    if not isinstance(subject, str):
        raise ValueError("notice payload: 'subject' must be text")
    if not isinstance(text, str):
        raise ValueError("notice payload: 'text' must be text")
    if html_body is not None and not isinstance(html_body, str):
        raise ValueError("notice payload: 'html' must be text or null")
    if not isinstance(headers, list | tuple):
        raise ValueError("notice payload: 'headers' must be a list of [name, value] pairs")
    pairs: list[tuple[str, str]] = []
    for entry in headers:
        if not (
            isinstance(entry, list | tuple) and len(entry) == 2 and all(isinstance(p, str) for p in entry)
        ):
            raise ValueError("notice payload: 'headers' must be a list of [name, value] pairs")
        name, value = entry
        if not _HEADER_NAME.fullmatch(name) or not _kept_header(name):
            raise ValueError(f"notice payload: header {name[:60]!r} is not allowed")
        pairs.append((name, value))
    # To and Subject go straight after From, where the builders put them, so the rebuilt email is identical
    after_from = next((i + 1 for i, (name, _) in enumerate(pairs) if name.lower() == "from"), 0)
    ordered = [*pairs[:after_from], ("To", ", ".join(to)), ("Subject", subject), *pairs[after_from:]]
    message = EmailMessage(policy=SMTP)
    try:
        for name, value in ordered:
            message[name] = value  # the email package refuses line breaks in a header value
        message.set_content(text)
        if html_body is not None:
            message.add_alternative(html_body, subtype="html")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"notice payload cannot be turned back into an email: {exc}") from exc
    return message


# ----------------------------------------------------------------------------- helpers
def _notice(
    settings: Settings,
    kind: str,
    *,
    to: Sequence[str],
    subject: str,
    in_reply_to: str | None,
    header: list[tuple[str, str]],
    sections: list[tuple[str, list[str]]],
    now: float | None,
) -> EmailMessage:
    """Headers in a fixed order (from_payload relies on From, To, Subject coming first), then both bodies."""
    secrets = _secrets(settings)
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr((FROM_NAME, settings.mail_from))
    message["To"] = ", ".join(to)
    message["Subject"] = _safe_subject(_scrub(_clean_header_text(subject), secrets))
    message["Date"] = formatdate(now, localtime=False)
    message["Message-ID"] = f"<relay.{kind}.{uuid.uuid4().hex}@{settings.mail_from_domain}>"
    reference = _reference(in_reply_to)
    if reference:
        message["In-Reply-To"] = reference
        message["References"] = reference
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message[RELAY_HEADER] = kind
    rows = [(label, _scrub(_clean_header_text(value), secrets)) for label, value in header]
    parts = [(title, [_scrub(line, secrets) for line in body]) for title, body in sections]
    message.set_content(_render_text(parts, rows, title="WhatsApp update"))
    message.add_alternative(_render_html(parts, rows, title="WhatsApp update"), subtype="html")
    return message


def _employee(address: str, name: str | None) -> list[str]:
    """The employee as the only recipient. A notice without a usable address cannot be sent: fail loudly."""
    cleaned = _clean_header_text(address)
    if not _ADDRESS.fullmatch(cleaned):
        raise ValueError(f"not a usable email address for a notice: {cleaned[:100]!r}")
    return [formataddr((_clean_header_text(name), cleaned))]


def _header_rows(
    settings: Settings,
    result: str,
    *,
    booking: str | None = None,
    when: float | None = None,
    when_label: str = "Sent",
    command_subject: str | None = None,
) -> list[tuple[str, str]]:
    rows = [("Result", result)]
    if booking:
        rows.append(("Booking", booking))
    if when:
        rows.append((when_label, _local_time(int(when), settings.mail_timezone)))
    subject = _clean_header_text(command_subject)
    if subject:
        rows.append(("Your email", subject))
    return rows


def _details(
    *,
    number: str,
    crm_match: str | None,
    text_title: str,
    customer_text: str | None,
    parameters: Sequence[str],
    template_name: str | None,
    template_language: str | None,
    files: list[str],
) -> list[tuple[str, list[str]]]:
    """The sections that describe the message itself, shared by the sent / dry-run / unclear notices."""
    to_lines = [f"Phone number: {number}"]
    if crm_match:
        to_lines.append(f"VShipCRM: {crm_match}")
    return [
        ("To", to_lines),
        (text_title, _customer_lines(customer_text, parameters)),
        ("Template", _template_lines(template_name, template_language)),
        ("Files", files),
    ]


def _customer_lines(customer_text: str | None, parameters: Sequence[str]) -> list[str]:
    # Indented as a quote, which also keeps a line of the employee's text from starting with "From ".
    lines = _lines(customer_text)
    if lines:
        return ["  " + line for line in lines]
    values = [" ".join(_lines(value)) for value in parameters]
    if values:
        return [
            "The template's text could not be shown here; these values were filled in:",
            *(f"  {{{{{index}}}}}: {value}" for index, value in enumerate(values, start=1)),
        ]
    return ["(not available)"]


def _template_lines(name: str | None, language: str | None) -> list[str]:
    if not name:
        return []
    return [f"Name: {name}", *([f"Language: {language}"] if language else [])]


def _file_results(files: Sequence[FileEntry]) -> list[FileResult]:
    results: list[FileResult] = []
    for entry in files:
        if not isinstance(entry, tuple | list):
            raise TypeError(f"a file result must be (filename, sent, reason), not {type(entry).__name__}")
        result = FileResult(*entry)
        if not (result.sent is None or isinstance(result.sent, bool)):
            raise TypeError(f"file result 'sent' must be True, False or None, not {result.sent!r}")
        results.append(result)
    return results


def _file_lines(
    results: Sequence[FileResult], *, dry_run: bool = False, default_reason: str | None = None
) -> list[str]:
    lines: list[str] = []
    for result in results:
        name = " ".join(_lines(result.filename)) or "(file without a name)"
        reason = " ".join(_lines(result.reason))
        if result.sent is True:
            label = "Would be sent" if dry_run else "Sent"
            lines.append(f"{label}: {name}" + (f" ({reason})" if reason else ""))
            continue
        if dry_run:
            label = "Would NOT be sent"
        elif result.sent is None:
            label = "MAY NOT have been sent"
        else:
            label = "NOT sent"
        reason = reason or default_reason or ""
        lines.append(f"{label}: {name}" + (f" - {reason}" if reason else ""))
    return lines


def _notes(
    extra: Sequence[str],
    *,
    name_fallback_used: bool = False,
    lines_joined: bool = False,
    test_mode: bool = False,
    dry_run: bool = False,
) -> list[str]:
    notes: list[str] = []
    if name_fallback_used:
        notes.append(
            "The Name: line was empty, so a general greeting was used instead of the customer's name."
        )
    if lines_joined:
        notes.append(
            "Your message had line breaks. WhatsApp does not allow them in this template, so the lines were"
            " joined with spaces."
        )
    if test_mode:
        notes.append(
            "Test mode is on: updates can only be sent to the test numbers the administrator set up."
        )
    if dry_run:
        notes.append(
            "The WhatsApp update service is still being tested: it only checks emails and sends nothing to"
            " customers yet."
        )
    notes.extend(line for text in extra for line in _lines(text))
    return notes


def _format_reminder(settings: Settings) -> list[str]:
    """The command format exactly as agreed with the owner (see app.relay.command)."""
    return [
        f"Write a new email to {_mailbox(settings)}. A reply or a forwarded email (RE:, FW:) is ignored.",
        "Subject:",
        "    WA | phone number | booking number",
        "    (the booking number may be left out: WA | phone number)",
        "Email text:",
        "    Name:customer name",
        "    Message:",
        "    the update for the customer (one or more lines)",
        "    ##",
        "Only the text between Message: and the line with only ## goes to the customer. Everything below ##"
        " (your signature, for example) is not sent.",
        "Phone number: 10 digits for a US number (for example 650-555-1234); a number in any other country"
        " with + and the country code (for example +44 20 7946 0958).",
        f"The name may have at most {settings.relay_max_name_chars:,} characters and the message at most"
        f" {settings.relay_max_message_chars:,}. Line breaks in the message are joined into one line.",
    ]


def _mailbox(settings: Settings) -> str:
    return settings.relay_mailbox_address or "the WhatsApp updates mailbox"


def _number(phone: str | None) -> str:
    return format_phone_for_humans(_clean_header_text(phone)) or "an unknown number"


def _lines(text: str | None) -> list[str]:
    """Caller text as body lines: one per line break, tabs as spaces, control characters removed."""
    if not text:
        return []
    return [_CONTROL.sub("", line.replace("\t", " ")) for line in str(text).splitlines()]


def _first_sentence(text: str) -> str:
    sentence = re.split(r"(?<=[.!?])\s", _clean_header_text(text), maxsplit=1)[0].rstrip(".!? ")
    if len(sentence) > _SHORT_REASON_MAX:
        sentence = sentence[: _SHORT_REASON_MAX - 3].rstrip() + "..."
    return sentence or "see the reason in this email"


def _reference(message_id: str | None) -> str | None:
    """The command's internetMessageId as a msg-id (``<...>``); None when empty or not shaped like one."""
    match = _MESSAGE_ID.fullmatch(_clean_header_text(message_id))
    if match is None:
        return None
    return f"<{match.group(1) or match.group(2)}>"


def _safe_subject(subject: str) -> str:
    """A subject starting with "WA" could pass for a command in the relay mailbox; never send one."""
    if subject[:2].lower() == "wa":
        return f"Notice: {subject}"
    return subject


def _secrets(settings: Settings) -> list[str]:
    values = {str(getattr(settings, name, "") or "").strip() for name in _SECRET_SETTINGS}
    return sorted((value for value in values if len(value) >= _MIN_SECRET_LENGTH), key=len, reverse=True)


def _scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, _REMOVED)
    return text


def _kept_header(name: str) -> bool:
    lowered = name.lower()
    return lowered not in _NOT_IN_HEADERS and not lowered.startswith("content-")


def _unix_lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
