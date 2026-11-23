"""``whatsapp-webhook relay-check`` and ``relay-parse``: is "WhatsApp updates from email" ready to work?

``relay-check`` tries, from the server, what the feature depends on:

* the settings (a summary, and the problems RELAY_ENABLED=true would refuse to start with),
* the relay mailbox: its Inbox can be read, and the folder for handled emails can be found (it is created
  if it is missing, as the service itself would on first use),
* every configured WhatsApp template: APPROVED, UTILITY, as many variables as the settings fill,
* the business number the updates are sent from: reachable, its display name status, quality and Meta's
  messaging limit,
* with ``--sample``: the newest command email from an allowed sender, in the Inbox or the folder - whether
  Exchange's internal-sender mark is visible on it, whether From and Sender match, whether its subject is
  still the Subject line it was delivered with, and how its text is read.

It only reads from Microsoft Graph and from Meta: it never sends an email or a WhatsApp message, and never
moves or changes an email (it does not even mark one as read). It runs while RELAY_ENABLED=false as well, so
everything can be checked before the feature is switched on.

Every line starts with ``ok``, ``WARN`` or ``FAIL``. Exit code: 0 everything is ready; 2 a check failed;
1 the feature is off or a setting a check needs is missing (and nothing failed). An environment that cannot
be loaded at all ends with 78 (configuration error), as for every command.

``relay-parse`` reads a subject and an email text offline, exactly as the service would, and prints the
result: for trying out the format with staff without sending anything. Exit code 0 when the email would be
taken, 2 when it would be refused (or is not a command at all).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.mail.compose import format_phone_for_humans
from app.mail.transports import GRAPH_SCOPE, OAuthTokenProvider, SendError
from app.relay.command import (
    CommandRefused,
    looks_like_command,
    normalize_target_phone,
    parse_body,
    parse_subject,
)
from app.relay.html import html_to_text
from app.relay.mailbox import IMMUTABLE_IDS, INBOX_PAGE, GraphMailbox, valid_address
from app.relay.mailinfo import MailInfo, auth_marks, automatic_mail, subject_edited
from app.relay.templates import (
    TemplateGuard,
    TemplateValueMissing,
    build_body_params,
    specs_from_settings,
)
from app.whatsapp.admin import graph_params
from app.whatsapp.send import WhatsAppSendError
from app.worker import mask_phone

EXIT_OK = 0
EXIT_NOT_READY = 1  # the feature is off, or a setting a check needs is missing
EXIT_FAILED = 2  # a check failed (relay-parse: the email would be refused)

EPOCH = "1970-01-01T00:00:00Z"
PHONE_FIELDS = (
    "display_phone_number,verified_name,name_status,quality_rating,whatsapp_business_manager_messaging_limit"
)
SAMPLE_DAYS = 30  # --sample looks at mail of the last 30 days
SAMPLE_INBOX_PAGES = 4
SAMPLE_FOLDER_TOP = 50
_NUMERIC_ID = re.compile(r"[0-9]{1,40}")
_TIER = re.compile(r"TIER_(\d+)(K?)", re.IGNORECASE)
_HTML_HINT = re.compile(r"<\s*(html|body|div|p|br|span|table)\b", re.IGNORECASE)
_GOOD_NAME_STATUS = {"APPROVED", "AVAILABLE_WITHOUT_REVIEW"}
_ROLES = {
    "update": "the update template",
    "booking": "the booking template (subjects with a booking number)",
    "document": "the document template (PDF files outside the 24-hour window)",
}


class Report:
    """Prints ``ok`` / ``WARN`` / ``FAIL`` lines and works out the exit code."""

    def __init__(self, out: Callable[[str], None]):
        self.out = out
        self.failed = False  # a check was made and failed
        self.incomplete = False  # a check could not be made: a setting it needs is missing
        self.feature_off = False

    def ok(self, text: str) -> None:
        self.out(f"ok    {text}")

    def warn(self, text: str) -> None:
        self.out(f"WARN  {text}")

    def fail(self, text: str) -> None:
        self.failed = True
        self.out(f"FAIL  {text}")

    def missing(self, text: str) -> None:
        """A setting is missing or wrong: FAIL (do not go on), but exit code 1 rather than 2."""
        self.incomplete = True
        self.out(f"FAIL  {text}")

    def exit_code(self) -> int:
        if self.failed:
            return EXIT_FAILED
        if self.incomplete or self.feature_off:
            return EXIT_NOT_READY
        return EXIT_OK


# ----------------------------------------------------------------------------- relay-check
async def run_check(
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    sample: bool = False,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
) -> int:
    report = Report(out)
    _check_settings(settings, report)
    mailbox = await _check_mailbox(settings, http, report)
    await _check_templates(settings, http, report)
    await _check_number(settings, http, report)
    if sample:
        if mailbox is None:
            report.warn("--sample skipped: the relay mailbox could not be read")
        else:
            await _check_sample(settings, http, mailbox, report, clock)
    return report.exit_code()


def _check_settings(settings: Settings, report: Report) -> None:
    problems = settings._relay_problems()
    if settings.relay_enabled:
        if problems:
            report.fail(
                "RELAY_ENABLED is true, but the service does NOT handle WhatsApp update emails until these"
                " settings are fixed and it is restarted (the forwarder runs as before)"
            )
        else:
            report.ok("RELAY_ENABLED is true: the service handles WhatsApp update emails")
    else:
        report.feature_off = True
        # A RELAY_ENABLED value that cannot be read leaves the feature off; its FAIL line below says why.
        unreadable = any(name == "relay_enabled" for name, _ in settings._relay_unreadable)
        state = "is not true" if unreadable else "is false"
        report.warn(f"RELAY_ENABLED {state}: the feature is off (checking everything anyway)")
    # With the feature on, any of these leaves the feature off (never the forwarder); show them either way.
    for problem in problems:
        report.missing(f"setting: {problem}")
    address = settings.relay_mailbox_address
    report.ok(f"relay mailbox: {address or '(not set)'}")
    allowed = settings.relay_allowed_sender_list
    if allowed:
        report.ok(f"allowed senders ({len(allowed)}): {', '.join(allowed)}")
    if settings.relay_require_internal_auth:
        report.ok(
            "only emails that Exchange marks as sent by a signed-in internal user count"
            " (RELAY_REQUIRE_INTERNAL_AUTH=true)"
        )
    else:
        report.warn(
            "RELAY_REQUIRE_INTERNAL_AUTH=false: Exchange's internal-sender mark is not required, so an"
            " email made to look as if it came from an allowed address could send updates"
        )
    if settings.relay_dry_run:
        report.ok("dry run (RELAY_DRY_RUN=true): emails are checked and answered, nothing goes to WhatsApp")
    else:
        report.ok("RELAY_DRY_RUN=false: updates are sent to WhatsApp")
    tests = settings.relay_test_number_list
    if tests:
        shown = ", ".join(mask_phone(number, settings.log_mask_phones) for number in tests)
        report.ok(f"test mode: updates only go to {len(tests)} test number(s): {shown}")
    else:
        report.ok("no test numbers: updates may go to any valid number")
    if settings.relay_sending_number_id:
        report.ok(f"updates are sent from the business number with id {settings.relay_sending_number_id}")
    report.ok(
        f'handled emails go to the folder "{settings.relay_folder}"; emails more than'
        f" {settings.relay_max_age_hours:g} hours old are refused"
    )


async def _check_mailbox(
    settings: Settings, http: httpx.AsyncClient, report: Report
) -> tuple[GraphMailbox, OAuthTokenProvider] | None:
    """Whether the Inbox can be read and the folder found; the mailbox for --sample if it can be read."""
    if settings.mail_transport != "graph":
        report.missing("mailbox not checked: MAIL_TRANSPORT must be graph (the Inbox is read through Graph)")
        return None
    if not (settings.oauth_tenant_id and settings.oauth_client_id and settings.oauth_client_secret):
        report.missing(
            "mailbox not checked: OAUTH_TENANT_ID, OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET are needed"
        )
        return None
    address = valid_address(settings.relay_mailbox_address)
    if address is None:
        report.missing("mailbox not checked: RELAY_MAILBOX (or GRAPH_SENDER) is not an email address")
        return None
    oauth = OAuthTokenProvider(http, settings)
    mailbox = GraphMailbox(http, settings, oauth, address)
    try:
        listed = await mailbox.list_inbox(EPOCH, pages=1)
    except SendError as exc:
        report.fail(f"mailbox {address} cannot be read: {exc}")
        return None
    count = f"{len(listed)} or more" if len(listed) >= INBOX_PAGE else str(len(listed))
    report.ok(f"mailbox {address} can be read ({count} email(s) in its Inbox)")
    folder = settings.relay_folder
    try:
        await mailbox.folder_id(folder)
    except SendError as exc:
        report.fail(f'folder "{folder}" cannot be found or created in {address}: {exc}')
    else:
        report.ok(f'folder "{folder}" exists in {address} (created if it was missing)')
    return mailbox, oauth


async def _check_templates(settings: Settings, http: httpx.AsyncClient, report: Report) -> None:
    if not (settings.wa_access_token and settings.wa_waba_id):
        report.missing("templates not checked: WA_ACCESS_TOKEN and WA_WABA_ID are needed")
        return
    guard = TemplateGuard(http, settings)
    specs = specs_from_settings(settings)
    for role in ("update", "booking", "document"):
        spec = specs[role]
        if spec is None:
            if role == "booking":
                report.ok(
                    "no booking template (RELAY_BOOKING_TEMPLATE_NAME is empty): updates with a booking"
                    " number use the update template"
                )
            else:
                report.ok(
                    "no document template (RELAY_DOCUMENT_TEMPLATE_NAME is empty): outside the 24-hour"
                    " window files are not sent"
                )
            continue
        label = f"template {spec.name or '(no name)'} ({spec.language}), {_ROLES[role]}"
        try:
            info = await guard.check(spec)
        except WhatsAppSendError as exc:
            report.fail(f"{label}: could not be checked: {exc}")
            continue
        if not info.ok:
            report.fail(f"{label}: {info.problem}")
            continue
        filled = ", ".join(spec.params) or "nothing"
        header = "; it has a document header" if spec.header == "document" else ""
        report.ok(
            f"{label}: {info.status}, {info.category}, {info.placeholders} variable(s) filled with"
            f" {filled}{header}"
        )


async def _check_number(settings: Settings, http: httpx.AsyncClient, report: Report) -> None:
    number_id = settings.relay_sending_number_id
    token = settings.wa_access_token
    if not token:
        report.missing("sending number not checked: WA_ACCESS_TOKEN is not set")
        return
    if not number_id:
        report.missing("sending number not checked: RELAY_PHONE_NUMBER_ID (or WA_PHONE_NUMBER_ID) is not set")
        return
    if not _NUMERIC_ID.fullmatch(number_id):
        report.missing(f"sending number not checked: {number_id[:40]!r} is not a numeric phone number id")
        return
    base = f"{settings.wa_graph_base_url.rstrip('/')}/{settings.wa_graph_api_version}"
    url = f"{base}/{quote(number_id, safe='')}"
    try:
        resp = await http.get(
            url,
            params=graph_params(token, settings.wa_app_secret, fields=PHONE_FIELDS),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        report.fail(f"WhatsApp could not be reached to read the sending number {number_id}: {exc!r}")
        return
    body = _json(resp)
    if resp.status_code != 200:
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = f", code {err['code']}" if err.get("code") is not None else ""
        said = str(err.get("message") or resp.text[:200] or "no explanation").strip().rstrip(".")
        report.fail(
            f"WhatsApp refused to show the sending number {number_id} (HTTP {resp.status_code}{code}):"
            f" {said}. Check the token and the phone number id with `whatsapp-webhook meta-check`"
        )
        return
    display = body.get("display_phone_number") or "?"
    name = body.get("verified_name") or "?"
    report.ok(f"sending number {display} ({name}) can be reached")
    name_status = str(body.get("name_status") or "").upper()
    if name_status in _GOOD_NAME_STATUS:
        report.ok(f"display name status: {name_status}")
    else:
        report.warn(
            f"display name status: {name_status or 'not given'}: until Meta approves the name, customers may"
            " see only the number"
        )
    quality = str(body.get("quality_rating") or "").upper()
    if quality in ("RED", "YELLOW"):
        report.warn(f"quality rating: {quality}: Meta may lower the messaging limit or block sending")
    else:
        report.ok(f"quality rating: {quality or 'not given'}")
    _report_limit(report, body.get("whatsapp_business_manager_messaging_limit"))


def _report_limit(report: Report, limit: Any) -> None:
    text = str(limit or "").strip()
    if text.upper() == "TIER_UNLIMITED":
        report.ok("messaging limit: TIER_UNLIMITED")
        return
    match = _TIER.fullmatch(text)
    if match is None:
        report.warn(f"messaging limit: {text or 'not given by Meta'}")
        return
    customers = int(match.group(1)) * (1000 if match.group(2) else 1)
    report.warn(
        f"messaging limit: {text.upper()}: updates can reach at most {customers:,} different customers per 24"
        " hours (customers who wrote to VShip in the last 24 hours do not count)"
    )


async def _check_sample(
    settings: Settings,
    http: httpx.AsyncClient,
    found: tuple[GraphMailbox, OAuthTokenProvider],
    report: Report,
    clock: Callable[[], float],
) -> None:
    mailbox, oauth = found
    allowed = set(settings.relay_allowed_sender_list)
    if not allowed:
        report.warn("--sample skipped: RELAY_ALLOWED_SENDERS is empty, so no email could be a command")
        return
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock() - SAMPLE_DAYS * 86400))
    folder = settings.relay_folder
    try:
        in_inbox = await mailbox.list_inbox(since, pages=SAMPLE_INBOX_PAGES)
    except SendError as exc:
        report.fail(f"--sample: the Inbox could not be searched: {exc}")
        return
    try:
        in_folder = await _list_folder(
            http, settings, oauth, mailbox.mailbox, await mailbox.folder_id(folder)
        )
    except SendError as exc:
        report.warn(f'--sample: the folder "{folder}" could not be searched, only the Inbox: {exc}')
        in_folder = []
    candidates = [  # (receivedDateTime, id, where)
        (str(message.get("receivedDateTime") or ""), str(message["id"]), where)
        for where, listed in (("Inbox", in_inbox), (f'folder "{folder}"', in_folder))
        for message in listed
        if _is_candidate(message, allowed)
    ]
    if not candidates:
        report.warn(
            f"--sample: no command email from an allowed sender in the last {SAMPLE_DAYS} days, in the Inbox"
            f' or in "{folder}". Send one (any test number) and run this again'
        )
        return
    received, message_id, where = max(candidates)
    try:
        raw = await mailbox.get_message(message_id)
    except SendError as exc:
        report.fail(f"--sample: the email could not be read: {exc}")
        return
    if raw is None:
        report.warn("--sample: the email disappeared while it was being read; run this again")
        return
    mail = MailInfo.from_graph(raw)
    report.ok(f"sample: the newest command email from an allowed sender, received {received} ({where})")
    _report_sender(settings, mail, report)
    text = html_to_text(mail.body) if mail.body_is_html else mail.body
    _report_parse(report, settings, mail.subject, text, show_phone=False)


def _report_sender(settings: Settings, mail: MailInfo, report: Report) -> None:
    """Rules 2 and 17: every one of these must pass, or the email is ignored (left in the Inbox)."""
    if mail.author is None:
        report.fail("sample: the From address cannot be read: such an email is ignored")
    elif mail.author == settings.relay_mailbox_address:
        report.fail("sample: it comes from the relay mailbox itself: such an email is ignored")
    elif mail.author in settings.relay_allowed_sender_list:
        report.ok(f"sample: From {mail.author} is on RELAY_ALLOWED_SENDERS")
    else:
        report.fail(f"sample: From {mail.author} is not on RELAY_ALLOWED_SENDERS: such an email is ignored")
    if mail.on_behalf:
        report.fail(
            f"sample: sent on behalf of someone else (Sender {mail.actual_sender or 'unreadable'}, From"
            f" {mail.author}): such an email is ignored"
        )
    else:
        report.ok("sample: From and Sender are the same person (not sent on behalf of someone else)")
    marks = auth_marks(mail)
    if marks and all(mark == "internal" for mark in marks):
        report.ok(
            "sample: Exchange's internal-sender mark is visible: X-MS-Exchange-Organization-AuthAs: Internal"
        )
    elif marks:
        shown = ", ".join(repr(mark) for mark in marks)
        report.fail(
            f"sample: Exchange's internal-sender mark says {shown}, not Internal: such an email is ignored"
        )
    elif settings.relay_require_internal_auth:
        report.fail(
            "sample: Graph does not show Exchange's internal-sender mark (X-MS-Exchange-Organization-AuthAs),"
            " so EVERY command would be ignored. Stop and report this (docs/whatsapp-updates-setup.md, 6.3)"
        )
    else:
        report.warn(
            "sample: Graph does not show Exchange's internal-sender mark (not required:"
            " RELAY_REQUIRE_INTERNAL_AUTH=false)"
        )
    # The check against emails edited in the mailbox after they arrived, tried on real Exchange data.
    if not mail.headers.get("subject"):
        report.warn(
            "sample: Graph does not show the Subject line it was delivered with, so a subject changed in the"
            " mailbox after an email arrived cannot be noticed"
        )
    elif subject_edited(mail):
        report.fail(
            "sample: its subject is not the Subject line it was delivered with (edited in the mailbox after"
            " it arrived): such an email is ignored"
        )
    else:
        report.ok("sample: its subject is the one it was delivered with")
    if mail.is_draft:
        report.fail(
            "sample: it is a draft, written in the mailbox rather than sent to it: such an email is ignored"
        )
    else:
        report.ok("sample: it was delivered to the mailbox (not a draft written in it)")
    reason = automatic_mail(mail)
    if reason:
        report.fail(f"sample: it counts as automatic mail ({reason}): such an email is ignored")
    else:
        report.ok("sample: an ordinary email, not automatic mail")


def _is_candidate(message: dict[str, Any], allowed: set[str]) -> bool:
    """A command-shaped subject from an allowed address, as the mailbox lists it (checked properly later)."""
    if not looks_like_command(str(message.get("subject") or "")):
        return False
    sender = message.get("from")
    inner = sender.get("emailAddress") if isinstance(sender, dict) else None
    address = valid_address(str(inner.get("address") or "")) if isinstance(inner, dict) else None
    return address in allowed


async def _list_folder(
    http: httpx.AsyncClient, settings: Settings, oauth: OAuthTokenProvider, address: str, folder_id: str
) -> list[dict[str, Any]]:
    """The newest emails of one folder (the mailbox module lists only the Inbox)."""
    url = (
        f"{settings.graph_base_url.rstrip('/')}/users/{quote(address, safe='@')}"
        f"/mailFolders/{quote(folder_id, safe='')}/messages"
    )
    params = {
        "$select": "id,receivedDateTime,from,subject",
        "$orderby": "receivedDateTime desc",
        "$top": str(SAMPLE_FOLDER_TOP),
    }
    token = await oauth.get_token(GRAPH_SCOPE)
    headers = {"Authorization": f"Bearer {token}", "Prefer": IMMUTABLE_IDS}
    try:
        resp = await http.get(url, params=params, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        raise SendError(f"Graph request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise SendError(f"Graph list folder returned HTTP {resp.status_code}: {resp.text[:300]}")
    return [m for m in _json(resp).get("value", []) if isinstance(m, dict) and m.get("id")]


# ----------------------------------------------------------------------------- relay-parse
def run_parse(
    settings: Settings,
    subject: str,
    body: str,
    *,
    html: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """Read ``subject`` and ``body`` the way the service reads a command email; nothing is sent."""
    report = Report(out)
    if not html and _HTML_HINT.search(body or ""):
        report.warn(
            "the text looks like HTML: add --html to read it the way the service reads Outlook's HTML"
        )
    text = html_to_text(body) if html else body
    taken = _report_parse(report, settings, subject, text, show_phone=True)
    return EXIT_OK if taken and not report.failed else EXIT_FAILED


def _report_parse(report: Report, settings: Settings, subject: str, text: str, *, show_phone: bool) -> bool:
    """Print how a command email is read; False when it would be refused (or is no command at all)."""
    if not looks_like_command(subject):
        report.fail(
            "not a command: the subject must start with WA | (with RE:, FW: or anything else in front the"
            " email is ignored and stays in the Inbox)"
        )
        return False
    try:
        parts = parse_subject(subject)
        phone = normalize_target_phone(parts.phone_raw, settings.relay_default_region)
        body = parse_body(
            text,
            max_name=settings.relay_max_name_chars,
            max_message=settings.relay_max_message_chars,
            fallback_name=settings.relay_name_fallback,
        )
    except CommandRefused as exc:
        report.fail(f"NOT sent ({exc.code}): {exc.text}")
        return False
    shown = (format_phone_for_humans(phone) or phone) if show_phone else mask_phone(phone)
    report.ok(f"phone number: {shown}")
    report.ok(f"booking number: {parts.booking}" if parts.booking else "no booking number")
    if body.name_was_empty:
        report.warn(f'the Name: line is empty: the greeting uses "{body.name}"')
    else:
        report.ok(f"name: {body.name}")
    report.ok(f"message ({len(body.message):,} characters): {body.message}")
    if body.lines_joined:
        report.warn("the message had line breaks: they are joined with spaces (the template allows none)")
    tests = settings.relay_test_number_list
    if tests and phone not in tests:
        report.warn("test mode is on and this is not a test number: the update would be refused")
    specs = specs_from_settings(settings)
    spec = specs["booking"] if parts.booking and specs["booking"] is not None else specs["update"]
    assert spec is not None
    try:
        values = build_body_params(spec, name=body.name, message=body.message, booking=parts.booking)
    except TemplateValueMissing as exc:
        report.fail(f"NOT sent: {exc.text}")
        return False
    filled = ", ".join(f"{{{{{index}}}}} = {value}" for index, value in enumerate(values, start=1))
    report.ok(f"template {spec.name} ({spec.language}): {filled or 'no variables'}")
    return True


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
