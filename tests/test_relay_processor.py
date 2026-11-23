"""RelayProcessor: the decision procedure, the step state that never sends twice, and what people are told."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx
from app.crm.directory import LookupResult, LookupStatus
from app.crm.phones import CrmRecord, MatchCandidate
from app.mail.transports import OAuthTokenProvider, SendError
from app.relay import processor as processor_module
from app.relay.files import (
    NOT_A_FILE,
    NOT_PDF_OUTSIDE_WINDOW,
    NOT_SUPPORTED,
    PASTED_PICTURES_NOTE,
    PDF_NO_TEMPLATE,
)
from app.relay.mailbox import GraphMailbox, MailAttachment
from app.relay.mailinfo import MailInfo, subject_edited
from app.relay.notices import _SHORT_REASONS, from_payload
from app.relay.processor import (
    GAVE_UP_FILE,
    GAVE_UP_SHORT,
    INTERRUPTED,
    RelayProcessor,
    crm_match_text,
)
from app.relay.templates import TemplateGuard, TemplateInfo, TemplateSpec
from app.store import (
    KIND_MESSAGE,
    KIND_NOTE,
    KIND_RELAY,
    KIND_RELAY_STATUS,
    STATUS_FAILED,
    STATUS_PENDING,
    QueueItem,
    Store,
)
from app.whatsapp.send import WhatsAppClient, WhatsAppSendError
from app.worker import Worker

from tests.conftest import FakeClock, make_settings, text_payload
from tests.test_worker import FakeTransport

DAY = 24 * 3600
EMPLOYEE = "elizabeth@vship2000.com"
MAILBOX = "whatsapp@vship2000.com"
PHONE = "+16505551234"
NUMBER = "+1 650-555-1234"  # as the employee's emails show it
NUMBER_ID = "106540352242922"
WABA = "102290129340398"
FOLDER = "WhatsApp updates - handled"
ALERT_TO = "ops@example.com, second@example.com"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# A complete relay configuration on top of the shared test settings (as in tests/test_relay_config.py).
RELAY_ON: dict[str, Any] = {
    "mail_transport": "graph",
    "graph_sender": MAILBOX,
    "oauth_tenant_id": "tenant-id",
    "oauth_client_id": "client-id",
    "oauth_client_secret": "client-secret",
    "wa_phone_number_id": NUMBER_ID,
    "wa_waba_id": WABA,
    "relay_enabled": True,
    "relay_allowed_senders": f"{EMPLOYEE}, ops@vship2000.com",
    "relay_dry_run": False,
}
UPDATE_BODY = (
    "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at"
    " Elizabeth@vship2000.com if you have any Questions"
)
BOOKING_BODY = (
    "Hello {{1}}, VShip has an update on booking {{2}}:\n{{3}}\nPlease reply if you have questions."
)
DOCUMENT_BODY = "Hello {{1}}, VShip sent you the document {{2}}."
BODIES = {"vship_update": UPDATE_BODY, "vship_booking_update": BOOKING_BODY, "vship_document": DOCUMENT_BODY}
LINES = (
    "Name:Maria",
    "Message:",
    "Your container has arrived at the port of Newark.",
    "Customs release is expected on Friday.",
    "##",
)
MESSAGE = "Your container has arrived at the port of Newark. Customs release is expected on Friday."
LOGO_CID = "image001.png@01DC5A11.1B2C3D40"

# ----------------------------------------------------------------------------- an email from Outlook
WORD_HEAD = (
    '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns="http://www.w3.org/TR/REC-html40"><head>'
    '<meta http-equiv="Content-Type" content="text/html; charset=us-ascii">'
    '<meta name="Generator" content="Microsoft Word 15 (filtered medium)">'
    "<style><!--\np.MsoNormal, li.MsoNormal, div.MsoNormal\n\t{margin:0in;\n\tfont-size:11.0pt;"
    '\n\tfont-family:"Aptos",sans-serif;}\nspan.EmailStyle17\n\t{mso-style-type:personal-compose;}\n--></style>'
    "</head>"
    '<body lang="EN-US" link="#467886" vlink="#96607D" style="word-wrap:break-word">'
    '<div class="WordSection1">'
)
SIGNATURE = (
    '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
    '<p class="MsoNormal"><b><span style="font-size:10.0pt">Elizabeth Reyes</span></b><o:p></o:p></p>'
    '<p class="MsoNormal"><span style="font-size:10.0pt">Operations | VShip | Mobile: 650-555-0100</span>'
    "<o:p></o:p></p>"
    '<p class="MsoNormal"><img width="120" height="40" style="width:1.25in;height:.4166in"'
    f' id="Picture_x0020_1" src="cid:{LOGO_CID}" alt="VShip"><o:p></o:p></p>'
)
WORD_TAIL = "</div></body></html>"


def outlook(*lines: str, extra: str = "") -> str:
    """The HTML body of a new email from Outlook for Windows: a paragraph per typed line, then a signature."""
    paragraphs = "".join(
        f'<p class="MsoNormal">{escape(line, quote=False)}<o:p></o:p></p>'
        if line
        else '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
        for line in lines
    )
    return WORD_HEAD + paragraphs + extra + SIGNATURE + WORD_TAIL


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_message(
    graph_id: str,
    *,
    subject: str,
    body: dict[str, str],
    author: str | None,
    name: str,
    sender: str | None,
    auth: tuple[str, ...],
    headers: tuple[tuple[str, str], ...],
    message_class: str | None,
    received: str,
    internet_id: str,
) -> dict[str, Any]:
    """What GraphMailbox.get_message returns for an email: Graph's JSON with the internet headers."""
    sender = author if sender is None else sender
    lines: list[tuple[str, str]] = [
        (
            "Received",
            "from SA1PR11MB8476.namprd11.prod.outlook.com (2603:10b6:806:3b0::14) by"
            " SA1PR11MB8475.namprd11.prod.outlook.com (2603:10b6:806:3af::9) with Microsoft SMTP Server"
            " (version=TLS1_2) id 15.20.7002.26; Tue, 14 Nov 2023 22:10:00 +0000",
        ),
        *([("From", f"{name} <{author}>")] if author else []),
        ("To", f"WhatsApp <{MAILBOX}>"),
        ("Subject", subject),
        ("Thread-Topic", subject),
        ("Thread-Index", "AdoXDQ6b4yH0x5Z3T0qkJ8Xq9m3W1g=="),
        ("Date", "Tue, 14 Nov 2023 22:09:58 +0000"),
        ("Message-ID", internet_id),
        ("Accept-Language", "en-US"),
        ("Content-Language", "en-US"),
        ("X-MS-Has-Attach", "yes"),
        ("X-MS-TNEF-Correlator", ""),
        *(("X-MS-Exchange-Organization-AuthAs", value) for value in auth),
        ("X-MS-Exchange-Organization-AuthMechanism", "04"),
        ("X-MS-Exchange-Organization-AuthSource", "SA1PR11MB8476.namprd11.prod.outlook.com"),
        *headers,
        ("MIME-Version", "1.0"),
    ]
    raw: dict[str, Any] = {
        "@odata.etag": 'W/"CQAAABYAAAD8o7Z3k9V8Q6U4ZRqVZ3m0AAAqDLjv"',
        "id": graph_id,
        "receivedDateTime": received,
        "hasAttachments": True,
        "internetMessageId": internet_id,
        "subject": subject,
        "body": body,
        "sender": {"emailAddress": {"name": name, "address": sender or ""}},
        "from": {"emailAddress": {"name": name, "address": author or ""}},
        "toRecipients": [{"emailAddress": {"name": "WhatsApp", "address": MAILBOX}}],
        "ccRecipients": [],
        "replyTo": [],
        "internetMessageHeaders": [{"name": k, "value": v} for k, v in lines],
    }
    if message_class is not None:
        raw["singleValueExtendedProperties"] = [{"id": "String 0x1a", "value": message_class}]
    return raw


def attachment(
    att_id: str,
    name: str,
    content_type: str,
    size: int = 1000,
    *,
    inline: bool = False,
    content_id: str | None = None,
    kind: str = "file",
) -> MailAttachment:
    return MailAttachment(att_id, name, content_type, size, inline, content_id, kind)


def matched() -> LookupResult:
    record = CrmRecord(
        entity="Customer",
        record_id="42",
        company="Acme Imports",
        contact="John Doe",
        active=True,
        updated_at=None,
        emails=["john@acme.com"],
    )
    candidate = MatchCandidate(record, "mobile", "650-555-1234", exact=True)
    return LookupResult(LookupStatus.MATCHED, candidates=[candidate], reply_to=["john@acme.com"])


def text_of(message: EmailMessage) -> str:
    part = message.get_body(("plain",))
    assert part is not None
    return part.get_content()


class Crash(Exception):
    """The service dying at that very moment (a power cut, a kill)."""


# ----------------------------------------------------------------------------- fakes
class FakeMailbox:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, list[MailAttachment]] = {}
        self.content: dict[str, bytes] = {}
        self.moved: list[tuple[str, str]] = []
        self.downloads: list[str] = []
        self.calls: list[str] = []
        self.reads: list[tuple[str, bool]] = []  # (message id, with its body) of every get_message
        self.failures: dict[str, list[Exception | None]] = {}

    def fail(self, method: str, *errors: Exception | None) -> None:
        """The next calls of ``method`` raise these errors in order (None: that call works)."""
        self.failures.setdefault(method, []).extend(errors)

    def _call(self, method: str) -> None:
        self.calls.append(method)
        queue = self.failures.get(method)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    async def get_message(self, message_id: str, *, with_body: bool = True) -> dict[str, Any] | None:
        self._call("get_message")
        self.reads.append((message_id, with_body))
        found = self.messages.get(message_id)
        if found is None:
            return None
        found = copy.deepcopy(found)
        if not with_body:  # as Graph answers without "body" in $select
            found.pop("body", None)
        return found

    async def list_attachments(self, message_id: str) -> list[MailAttachment]:
        self._call("list_attachments")
        return list(self.attachments.get(message_id, []))

    async def download_attachment(self, message_id: str, att: MailAttachment, dest: Path) -> int:
        self._call("download_attachment")
        data = self.content[att.id]
        await asyncio.to_thread(dest.write_bytes, data)
        self.downloads.append(att.id)
        return len(data)

    async def move(self, message_id: str, folder_name: str) -> None:
        self._call("move")
        self.moved.append((message_id, folder_name))


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []  # what reached WhatsApp
        self.attempts: list[str] = []  # every send call, refused ones included
        self.uploads: list[tuple[str, str, bytes]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.crash_after: dict[str, Exception] = {}  # raised right after a message of that type went out
        self.upload_errors: list[Exception] = []

    def fail(self, kind: str, *errors: Exception) -> None:
        self.failures.setdefault(kind, []).extend(errors)

    def _send(self, **message: Any) -> str:
        kind = message["type"]
        self.attempts.append(kind)
        queue = self.failures.get(kind)
        if queue:
            raise queue.pop(0)
        wamid = f"wamid.OUT{len(self.sent) + 1}"
        self.sent.append({**message, "wamid": wamid})
        crash = self.crash_after.pop(kind, None)
        if crash is not None:
            raise crash
        return wamid

    async def send_template(self, number_id, *, phone, name, language, body_params, header_document=None):
        return self._send(
            type="template",
            number_id=number_id,
            phone=phone,
            name=name,
            language=language,
            body_params=list(body_params),
            header_document=header_document,
        )

    async def send_media(self, number_id, *, phone, bsuid, kind, media_id, filename=None, reply_to=None):
        assert bsuid is None and reply_to is None
        return self._send(type=kind, number_id=number_id, phone=phone, media_id=media_id, filename=filename)

    async def upload_media(self, number_id, path: Path, mime_type, filename):
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        assert number_id == NUMBER_ID
        assert path.name.endswith(".bin")  # never the sender's file name on disk
        self.uploads.append((filename, mime_type, await asyncio.to_thread(path.read_bytes)))
        return f"media{len(self.uploads)}"


class FakeGuard:
    def __init__(self) -> None:
        self.infos: dict[str, TemplateInfo] = {}
        self.checked: list[str] = []
        self.forgotten: list[str | None] = []
        self.errors: list[Exception] = []

    async def check(self, spec: TemplateSpec) -> TemplateInfo:
        self.checked.append(spec.name)
        if self.errors:
            raise self.errors.pop(0)
        if spec.name in self.infos:
            return self.infos[spec.name]
        return TemplateInfo(
            ok=True,
            status="APPROVED",
            category="UTILITY",
            body_text=BODIES.get(spec.name),
            placeholders=len(spec.params),
            problem=None,
        )

    def forget(self, spec: TemplateSpec | None = None) -> None:
        self.forgotten.append(spec.name if spec else None)


class FakeDirectory:
    def __init__(self, result: LookupResult | None = None) -> None:
        self.result = result or LookupResult(
            LookupStatus.NO_MATCH, reason="no CRM record has phone +16505551234"
        )
        self.error: Exception | None = None
        self.looked_up: list[str] = []

    async def lookup(self, phone: str) -> LookupResult:
        self.looked_up.append(phone)
        if self.error is not None:
            raise self.error
        return self.result


class Rig:
    """A RelayProcessor on a real store, with fake mailbox, WhatsApp, template guard and CRM."""

    def __init__(self, tmp_path: Path, clock: FakeClock, **overrides: Any):
        self.settings = make_settings(tmp_path, **{**RELAY_ON, **overrides})
        self.store = Store(self.settings.store_path)
        self.clock = clock
        self.mailbox = FakeMailbox()
        self.whatsapp = FakeWhatsApp()
        self.guard = FakeGuard()
        self.directory = FakeDirectory()
        self.wakeup = asyncio.Event()
        self.count = 0
        self.inbound = 0
        self.restart()

    def restart(self, **changes: Any) -> RelayProcessor:
        """A new processor on the same store, as after a restart (``changes``: edited settings)."""
        if changes:
            self.settings = self.settings.model_copy(update=changes)
        self.processor = RelayProcessor(
            self.settings,
            self.store,
            self.mailbox,
            self.whatsapp,
            self.guard,
            self.directory,
            clock=self.clock,
        )
        self.processor.wakeup = self.wakeup
        return self.processor

    def email(
        self,
        subject: str = "WA | 650-555-1234",
        lines: tuple[str, ...] = LINES,
        *,
        html: str | None = None,
        plain: str | None = None,
        author: str | None = EMPLOYEE,
        name: str = "Elizabeth Reyes",
        sender: str | None = None,
        auth: tuple[str, ...] = ("Internal",),
        headers: tuple[tuple[str, str], ...] = (),
        message_class: str | None = "IPM.Note",
        age: float = 200.0,
        attachments: tuple[tuple[MailAttachment, bytes], ...] | list[tuple[MailAttachment, bytes]] = (),
    ) -> QueueItem:
        """Put an email into the fake mailbox and queue it the way the poller does."""
        self.count += 1
        graph_id = f"AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAD{self.count:04d}="
        received = iso(self.clock() - age)
        if plain is not None:
            body = {"contentType": "text", "content": plain}
        else:
            body = {"contentType": "html", "content": html if html is not None else outlook(*lines)}
        self.mailbox.messages[graph_id] = graph_message(
            graph_id,
            subject=subject,
            body=body,
            author=author,
            name=name,
            sender=sender,
            auth=auth,
            headers=headers,
            message_class=message_class,
            received=received,
            internet_id=f"<SA1PR11MB8476{self.count:04d}@SA1PR11MB8476.namprd11.prod.outlook.com>",
        )
        self.mailbox.attachments[graph_id] = [att for att, _ in attachments]
        self.mailbox.content.update({att.id: data for att, data in attachments})
        key = f"relay:{graph_id}"
        assert self.store.enqueue(KIND_RELAY, key, {"id": graph_id, "received": received}, self.clock())
        item = self.store.get(key)
        assert item is not None
        return item

    def customer_wrote(
        self, seconds_ago: float = 3600, *, phone: str = "16505551234", number_id: str = NUMBER_ID
    ) -> None:
        """A WhatsApp message from the customer, queued by the webhook (it opens the 24-hour window)."""
        self.inbound += 1
        wamid = f"wamid.IN{self.inbound}"
        payload = text_payload(wamid=wamid, from_phone=phone)
        value = payload["entry"][0]["changes"][0]["value"]
        value["metadata"]["phone_number_id"] = number_id
        value["messages"][0]["timestamp"] = str(int(self.clock() - seconds_ago))
        assert self.store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, self.clock() - seconds_ago + 2)

    def status_item(
        self, wamid: str, status: str = "failed", code: int | None = 131026, **error: Any
    ) -> QueueItem:
        """A KIND_RELAY_STATUS item as the webhook queues it."""
        self.inbound += 1
        errors = [{"code": code, "title": None, "message": None, "details": None, **error}] if code else []
        payload = {
            "wamid": wamid,
            "status": status,
            "timestamp": int(self.clock()),
            "recipient_id": "16505551234",
            "errors": errors,
        }
        key = f"relay_status:{wamid}:{self.inbound}"
        assert self.store.enqueue(KIND_RELAY_STATUS, key, payload, self.clock())
        item = self.store.get(key)
        assert item is not None
        return item

    async def run(self, item: QueueItem) -> int:
        current = self.store.get(item.key)
        assert current is not None
        return await self.processor.process(current)

    def notes(self) -> list[tuple[str, EmailMessage]]:
        """Every email the feature queued, oldest first: (queue key, the email)."""
        items = [i for i in self.store.list_by_status(STATUS_PENDING, 10_000) if i.kind == KIND_NOTE]
        return [(i.key, from_payload(i.payload)) for i in sorted(items, key=lambda i: i.id)]

    def state(self, item: QueueItem) -> dict[str, Any] | None:
        return self.store.get_relay_state(item.key)

    @staticmethod
    def graph_id(item: QueueItem) -> str:
        return item.payload["id"]


def statuses(rig: Rig, item: QueueItem) -> dict[str, str]:
    state = rig.state(item)
    assert state is not None
    return {step_id: step["status"] for step_id, step in state["steps"].items()}


def spool_left(rig: Rig) -> list[Path]:
    return list(Path(rig.settings.spool_dir).glob("relay_*"))


# ----------------------------------------------------------------------------- sending an update
async def test_a_command_sends_the_update_and_tells_the_employee(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    assert await rig.run(item) == 1
    assert rig.whatsapp.sent == [
        {
            "type": "template",
            "number_id": NUMBER_ID,
            "phone": PHONE,
            "name": "vship_update",
            "language": "en_US",
            "body_params": ["Maria", MESSAGE],
            "header_document": None,
            "wamid": "wamid.OUT1",
        }
    ]
    record = rig.store.get_relay_sent("wamid.OUT1")
    assert record is not None and (record["item_key"], record["what"]) == (item.key, "update")
    [(key, notice)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result"
    assert notice["X-WhatsApp-Relay"] == "sent"
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER}"
    assert notice["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert notice["In-Reply-To"] == "<SA1PR11MB84760001@SA1PR11MB8476.namprd11.prod.outlook.com>"
    text = text_of(notice)
    # the template's text as the customer sees it
    assert "  Hello Maria, VShip has an update:\n  " + MESSAGE + "\n  Please either reply" in text
    assert "VShipCRM: no CRM customer has this number" in text
    assert "Name: vship_update" in text and "Language: en_US" in text
    assert "the lines were joined with spaces" in text
    assert PASTED_PICTURES_NOTE not in text  # the signature logo was not among the attachments
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    assert rig.wakeup.is_set()
    assert statuses(rig, item) == {"template": "sent", "notice": "sent", "filed": "sent"}
    assert rig.directory.looked_up == [PHONE]


async def test_a_plain_text_email_works_too(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email(plain="Name:\r\nMaria\r\nMessage: It has arrived.\r\n##\r\nElizabeth")
    assert await rig.run(item) == 1
    assert rig.whatsapp.sent[0]["body_params"] == ["Maria", "It has arrived."]


async def test_an_empty_name_greets_with_the_fallback_word(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    assert await rig.run(rig.email(lines=("Name:", "Message:", "It has arrived.", "##"))) == 1
    assert rig.whatsapp.sent[0]["body_params"] == ["there", "It has arrived."]
    text = text_of(rig.notes()[0][1])
    assert "Hello there, VShip has an update:" in text
    assert "a general greeting was used" in text


async def test_a_finished_email_is_never_handled_again(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    await rig.run(item)
    calls = list(rig.mailbox.calls)
    rig.restart()
    assert await rig.run(item) == 1  # what this email sent in all; nothing new
    assert len(rig.whatsapp.attempts) == 1 and len(rig.notes()) == 1 and len(rig.mailbox.moved) == 1
    assert rig.mailbox.calls == calls  # not even read again: the decision is stored


# ----------------------------------------------------------------------------- a. to e.: not commands
async def test_an_email_that_is_gone_is_done_without_a_trace(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    del rig.mailbox.messages[rig.graph_id(item)]
    assert await rig.run(item) == 0
    assert rig.notes() == [] and rig.mailbox.moved == [] and rig.whatsapp.attempts == []
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "gone"


@pytest.mark.parametrize(
    "subject",
    [
        "RE: WA | 650-555-1234",
        "FW: WA | 650-555-1234",
        "AW: WA | 650-555-1234",
        "WG: WA|6505551234",
        "SV: WA | 650-555-1234",
        "Automatic reply: WA | 650-555-1234",
        "Fwd WA | 650-555-1234",
        "Re WA update",
        "",
    ],
)
async def test_replies_forwards_and_other_subjects_are_left_untouched(tmp_path, clock, subject):
    rig = Rig(tmp_path, clock)
    item = rig.email(subject)
    assert await rig.run(item) == 0
    assert rig.notes() == [] and rig.mailbox.moved == [] and rig.whatsapp.attempts == []
    assert rig.guard.checked == []
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "ignore"


@pytest.mark.parametrize(
    ("message_class", "headers"),
    [
        ("IPM.Note.Rules.OofTemplate.Microsoft", ()),  # an out-of-office reply
        ("REPORT.IPM.Note.NDR", ()),  # a bounce
        ("IPM.Note", (("Auto-Submitted", "auto-replied"),)),
        (
            "IPM.Note",
            (("X-MS-Mail-Application", "Microsoft Power Automate; User-Agent: azure-logic-apps/1.0"),),
        ),
        ("IPM.Note", (("Precedence", "bulk"),)),
        ("IPM.Note", (("List-Id", "<ops.vship2000.com>"),)),
    ],
)
async def test_automatic_mail_is_never_a_command(tmp_path, clock, message_class, headers):
    rig = Rig(tmp_path, clock)
    item = rig.email(message_class=message_class, headers=headers)
    assert await rig.run(item) == 0
    assert rig.notes() == [] and rig.mailbox.moved == [] and rig.whatsapp.attempts == []
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "ignore"


@pytest.mark.parametrize("author", [None, MAILBOX])
async def test_an_email_without_a_readable_sender_or_from_the_relay_mailbox_is_ignored(
    tmp_path, clock, author
):
    rig = Rig(tmp_path, clock)
    item = rig.email(author=author)
    assert await rig.run(item) == 0
    assert rig.notes() == []  # no digest either
    assert rig.mailbox.moved == [] and rig.whatsapp.attempts == []
    assert rig.mailbox.reads == [(rig.graph_id(item), False)]  # decided without reading its text
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "ignore"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"author": "mallory@vship2000.com"}, "the sender is not on RELAY_ALLOWED_SENDERS"),
        ({"auth": ()}, "Exchange's sign-in mark (X-MS-Exchange-Organization-AuthAs) is missing"),
        ({"auth": ("Anonymous",)}, "(AuthAs: anonymous)"),
        ({"auth": ("Internal", "Anonymous")}, "(AuthAs: internal, anonymous)"),
        ({"sender": "assistant@vship2000.com"}, "sent on behalf of someone else"),
    ],
)
async def test_commands_from_untrusted_senders_stay_in_the_inbox_and_alert_the_administrators(
    tmp_path, clock, overrides, why
):
    rig = Rig(tmp_path, clock)
    item = rig.email(**overrides)
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.mailbox.moved == [] and rig.guard.checked == []
    # Decided without its text: a stranger's long email is never downloaded (one small read only).
    assert rig.mailbox.reads == [(rig.graph_id(item), False)]
    [(key, alert)] = rig.notes()  # nobody but the administrators hears about it
    assert key.startswith("note:relay_alert:not_allowed:")
    assert alert["To"] == ALERT_TO
    assert alert["Subject"] == "[WhatsApp] ALERT: Emails from senders who may not send WhatsApp updates"
    assert alert["X-WhatsApp-Relay"] == "alert"
    text = text_of(alert)
    assert why in text
    assert (overrides.get("author") or EMPLOYEE) in text
    assert "650" not in text  # neither the subject nor the text of the email is repeated
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "not_allowed"
    assert why in state["decision"]["why"]


async def test_a_trusted_command_is_read_in_full_only_after_its_sender_passed(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    assert await rig.run(item) == 1
    graph_id = rig.graph_id(item)
    assert rig.mailbox.reads == [(graph_id, False), (graph_id, True)]


@pytest.mark.parametrize("change", ["subject edited", "deleted"])
async def test_an_email_changed_between_the_two_reads_is_decided_as_read_the_second_time(
    tmp_path, clock, monkeypatch, change
):
    """Someone with write access to the mailbox edits (or deletes) the email right after its sender passed."""
    rig = Rig(tmp_path, clock)
    item = rig.email()
    read = rig.mailbox.get_message

    async def changed_meanwhile(message_id: str, *, with_body: bool = True) -> dict[str, Any] | None:
        found = await read(message_id, with_body=with_body)
        if not with_body and change == "deleted":
            del rig.mailbox.messages[message_id]
        elif not with_body:
            rig.mailbox.messages[message_id]["subject"] = "WA | +1 202 555 0100"
        return found

    monkeypatch.setattr(rig.mailbox, "get_message", changed_meanwhile)
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.guard.checked == [] and rig.mailbox.moved == []
    state = rig.state(item)
    assert state is not None
    if change == "deleted":
        assert state["decision"]["action"] == "gone" and rig.notes() == []
    else:
        assert state["decision"]["action"] == "not_allowed"
        assert "its subject is not the one it arrived with" in state["decision"]["why"]
        [(key, _)] = rig.notes()
        assert key.startswith("note:relay_alert:not_allowed:")


async def test_a_draft_written_in_the_mailbox_is_not_a_command(tmp_path, clock):
    """Someone with write access to the mailbox writes a "received" command there, with forged headers."""
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.mailbox.messages[rig.graph_id(item)]["isDraft"] = True
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.guard.checked == [] and rig.mailbox.moved == []
    [(key, alert)] = rig.notes()  # the administrators hear about it; the employee does not
    assert key.startswith("note:relay_alert:not_allowed:")
    assert f"{EMPLOYEE} - it is a draft, written in the mailbox rather than sent to it" in text_of(alert)
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "not_allowed"
    delivered = rig.email()
    rig.mailbox.messages[rig.graph_id(delivered)]["isDraft"] = False  # what Graph says of a delivered email
    assert await rig.run(delivered) == 1


async def test_the_text_is_read_off_the_event_loop_so_the_webhook_keeps_answering(
    tmp_path, clock, monkeypatch
):
    rig = Rig(tmp_path, clock)
    threads: dict[str, int] = {}
    for name in ("html_to_text", "parse_body"):
        original = getattr(processor_module, name)

        def recorded(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            threads[_name] = threading.get_ident()
            return _original(*args, **kwargs)

        monkeypatch.setattr(processor_module, name, recorded)
    assert await rig.run(rig.email()) == 1
    assert set(threads) == {"html_to_text", "parse_body"}
    assert threading.get_ident() not in threads.values()  # this is the event loop's thread


async def test_without_required_internal_auth_only_a_missing_mark_is_accepted(tmp_path, clock):
    rig = Rig(tmp_path, clock, relay_require_internal_auth=False)
    assert await rig.run(rig.email(auth=())) == 1
    item = rig.email(auth=("Anonymous",))  # a mark that says anything else is still distrusted
    assert await rig.run(item) == 0
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "not_allowed"


def delivered_with(rig: Rig, item: QueueItem, subject: str) -> None:
    """The Subject line Exchange delivered the email with (kept in its internet headers)."""
    for header in rig.mailbox.messages[rig.graph_id(item)]["internetMessageHeaders"]:
        if header["name"] in ("Subject", "Thread-Topic"):
            header["value"] = subject


async def test_an_email_edited_in_the_mailbox_after_it_arrived_is_not_a_command(tmp_path, clock):
    """Someone with write access to the mailbox turns an employee's ordinary email into a command."""
    rig = Rig(tmp_path, clock)
    item = rig.email("WA | +1 202 555 0100", ("Name:X", "Message:", "Pay here: https://pay.example", "##"))
    delivered_with(rig, item, "RE: WhatsApp update sent: +1 650-555-1234")  # a reply to a confirmation
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.mailbox.moved == [] and rig.guard.checked == []
    [(key, alert)] = rig.notes()  # the administrators hear about it; the employee does not
    assert key.startswith("note:relay_alert:not_allowed:")
    text = text_of(alert)
    assert f"{EMPLOYEE} - its subject is not the one it arrived with" in text
    assert "555 0100" not in text and "pay.example" not in text  # neither its subject nor its text
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "not_allowed"


@pytest.mark.parametrize(
    ("subject", "delivered"),
    [
        ("WA | 650-555-1234", "=?utf-8?B?V0EgfCA2NTAtNTU1LTEyMzQ=?="),  # an encoded word
        ("WA | 650-555-1234", "=?utf-8?Q?WA_=7C_650-555-1234?="),
        ("WA | 650-555-1234", "=?utf-8?B?V0EgfCA2NTA=?=\r\n =?utf-8?B?LTU1NS0xMjM0?="),  # folded
        (f"WA | 650-555-1234 | BK{chr(0x2013)}1", "=?utf-8?Q?WA_|_650-555-1234_|_BK=E2=80=931?="),  # dash
        ("WA | 650-555-1234", "wa |  650-555-1234 "),  # spaces and letter case
    ],
)
async def test_the_delivered_subject_is_compared_as_a_mail_program_shows_it(
    tmp_path, clock, subject, delivered
):
    rig = Rig(tmp_path, clock)
    item = rig.email(subject)
    delivered_with(rig, item, delivered)
    assert await rig.run(item) == 1


def test_without_a_delivered_subject_line_there_is_nothing_to_compare():
    raw = {"subject": "WA | 650-555-1234", "internetMessageHeaders": [{"name": "To", "value": MAILBOX}]}
    assert not subject_edited(MailInfo.from_graph(raw))
    raw["internetMessageHeaders"].append({"name": "Subject", "value": "Lunch on Friday?"})
    assert subject_edited(MailInfo.from_graph(raw))


async def test_the_not_allowed_digest_alerts_at_once_then_at_most_once_a_day(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    await rig.run(rig.email(author="mallory@vship2000.com"))
    assert len(rig.notes()) == 1  # the first one at once
    clock.advance(3600)
    await rig.run(rig.email(author="trudy@vship2000.com"))
    clock.advance(3600)
    await rig.run(rig.email(author="eve@vship2000.com"))
    rig.processor.flush_digests()
    assert len(rig.notes()) == 1  # collected, not sent yet
    clock.advance(22 * 3600)  # 24 hours after the first alert
    rig.processor.flush_digests()
    notes = rig.notes()
    assert len(notes) == 2
    text = text_of(notes[1][1])
    assert "trudy@vship2000.com" in text and "eve@vship2000.com" in text
    assert "mallory@vship2000.com" not in text  # listed in the first alert already
    rig.processor.flush_digests()
    assert len(rig.notes()) == 2  # nothing left to send
    # the next one within 24 hours is collected again ...
    clock.advance(3600)
    await rig.run(rig.email(author="mallory@vship2000.com"))
    rig.processor.flush_digests()
    assert len(rig.notes()) == 2
    # ... and one arriving more than 24 hours after the last alert goes at once, with the collected one
    clock.advance(DAY)
    await rig.run(rig.email(author="oscar@vship2000.com"))
    notes = rig.notes()
    assert len(notes) == 3
    text = text_of(notes[2][1])
    assert "mallory@vship2000.com" in text and "oscar@vship2000.com" in text
    assert len({key for key, _ in notes}) == 3


async def test_the_digest_lists_at_most_50_emails_and_counts_the_rest(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    await rig.run(rig.email(author="first@example.org"))
    for index in range(55):
        await rig.run(rig.email(author=f"spam{index}@example.org"))
    stored = json.loads(rig.store.get_meta("relay_not_allowed") or "{}")
    assert len(stored["pending"]) == 50 and stored["more"] == 5
    clock.advance(DAY)
    rig.processor.flush_digests()
    _, digest = rig.notes()[-1]
    text = text_of(digest)
    assert "spam0@example.org" in text and "spam49@example.org" in text
    assert "spam50@example.org" not in text
    assert "- ... and 5 more." in text
    assert json.loads(rig.store.get_meta("relay_not_allowed") or "{}") == {
        "last_alert_at": clock(),
        "pending": [],
        "more": 0,
    }


async def test_a_broken_digest_record_does_not_stop_anything(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.store.set_meta("relay_not_allowed", "{not json")
    await rig.run(rig.email(author="mallory@vship2000.com"))
    assert len(rig.notes()) == 1
    rig.store.set_meta("relay_not_allowed", json.dumps({"last_alert_at": "x", "pending": "x", "more": -3}))
    rig.processor.flush_digests()
    assert len(rig.notes()) == 1


# ----------------------------------------------------------------------------- f. to h.: refusals
async def test_an_email_older_than_24_hours_is_refused_and_filed(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email(age=DAY + 60)
    assert await rig.run(item) == 0
    [(_, notice)] = rig.notes()
    assert notice["X-WhatsApp-Relay"] == "refused"
    assert notice["Subject"] == "WhatsApp update NOT sent: your email is too old"
    assert "more than 24 hours ago" in text_of(notice)
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    assert rig.whatsapp.attempts == [] and rig.guard.checked == []


@pytest.mark.parametrize(
    ("subject", "lines", "code"),
    [
        ("WA | 650-555-1234 | BK1 | extra", LINES, "subject_fields"),
        ("WA |  | BK1", LINES, "phone_missing"),
        ("WA | 12345", LINES, "phone_invalid"),
        ("WA | 0044 20 7946 0958", LINES, "phone_prefix"),
        ("WA | 650-555-1234", ("Hello Maria,", "Message:", "Text", "##"), "name_missing"),
        ("WA | 650-555-1234", ("Name:" + "N" * 61, "Message:", "Text", "##"), "name_too_long"),
        ("WA | 650-555-1234", ("Name:Maria", "Your container has arrived.", "##"), "message_missing"),
        ("WA | 650-555-1234", ("Name:Maria", "Message:", "Your container has arrived."), "end_missing"),
        ("WA | 650-555-1234", ("Name:Maria", "Message:", "##"), "message_empty"),
        ("WA | 650-555-1234", ("Name:Maria", "Message:", "x" * 1001, "##"), "message_too_long"),
    ],
)
async def test_every_broken_command_is_refused_with_its_reason(tmp_path, clock, subject, lines, code):
    rig = Rig(tmp_path, clock)
    item = rig.email(subject, lines)
    assert await rig.run(item) == 0
    [(key, notice)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result"
    assert notice["X-WhatsApp-Relay"] == "refused"
    assert notice["Subject"] == f"WhatsApp update NOT sent: {_SHORT_REASONS[code]}"
    assert "How to write a WhatsApp update email" in text_of(notice)
    state = rig.state(item)
    assert state is not None and state["decision"]["refusal"]["code"] == code
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    assert rig.whatsapp.attempts == []


async def test_test_mode_sends_only_to_the_test_numbers(tmp_path, clock):
    rig = Rig(tmp_path, clock, relay_test_numbers="(650) 555-0000, +44 20 7946 0958")
    other = rig.email("WA | 650-555-1234")
    assert await rig.run(other) == 0
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == "WhatsApp update NOT sent: test mode is on (not a test number)"
    assert f"{NUMBER} is not one of them" in text_of(notice)
    assert rig.mailbox.moved == [(rig.graph_id(other), FOLDER)]
    assert rig.whatsapp.attempts == []
    allowed = rig.email("WA | +44 20 7946 0958")
    assert await rig.run(allowed) == 1
    assert rig.whatsapp.sent[0]["phone"] == "+442079460958"
    assert "Test mode is on" in text_of(rig.notes()[-1][1])


# ----------------------------------------------------------------------------- i.: the template
async def test_a_booking_number_without_a_booking_template_goes_with_the_update_template(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    assert await rig.run(rig.email("WA | 650-555-1234 | BK12345")) == 1
    assert (rig.whatsapp.sent[0]["name"], rig.whatsapp.sent[0]["body_params"]) == (
        "vship_update",
        ["Maria", MESSAGE],
    )
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER} (booking BK12345)"
    assert "The booking number BK12345 is only shown in this email" in text_of(notice)


async def test_the_booking_template_needs_both_a_booking_number_and_the_setting(tmp_path, clock):
    rig = Rig(
        tmp_path,
        clock,
        relay_booking_template_name="vship_booking_update",
        relay_booking_template_params="name,booking,message",
    )
    await rig.run(rig.email("WA | 650-555-1234 | BK12345"))
    await rig.run(rig.email("WA | 650-555-1234 |"))  # an empty booking field is no booking number
    first, second = rig.whatsapp.sent
    assert (first["name"], first["body_params"]) == ("vship_booking_update", ["Maria", "BK12345", MESSAGE])
    assert (second["name"], second["body_params"]) == ("vship_update", ["Maria", MESSAGE])
    text = text_of(rig.notes()[0][1])
    assert "Hello Maria, VShip has an update on booking BK12345:" in text
    assert "only shown in this email" not in text


async def test_a_template_meta_does_not_allow_refuses_the_update_and_alerts_once_a_day(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.guard.infos["vship_update"] = TemplateInfo(
        ok=False,
        status="APPROVED",
        category="MARKETING",
        body_text=UPDATE_BODY,
        placeholders=2,
        problem="WhatsApp template vship_update (en_US) cannot be used. Its category at Meta is MARKETING.",
    )
    first = rig.email()
    assert await rig.run(first) == 0
    notes = rig.notes()
    assert [n["X-WhatsApp-Relay"] for _, n in notes] == ["alert", "refused"]
    assert notes[0][0].startswith("note:relay_alert:template:vship_update:")
    assert notes[0][1]["To"] == ALERT_TO
    assert notes[0][1]["Subject"] == "[WhatsApp] ALERT: WhatsApp template vship_update cannot be used"
    assert "MARKETING" in text_of(notes[0][1])
    assert notes[1][1]["Subject"] == "WhatsApp update NOT sent: the WhatsApp template is not ready"
    assert "MARKETING" in text_of(notes[1][1])
    assert rig.mailbox.moved == [(rig.graph_id(first), FOLDER)]
    clock.advance(3600)  # the same day: refused, but not alerted again
    await rig.run(rig.email())
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()] == ["alert", "refused", "refused"]
    clock.advance(DAY)  # a day later: alerted again
    await rig.run(rig.email())
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()] == [
        "alert",
        "refused",
        "refused",
        "alert",
        "refused",
    ]
    assert rig.whatsapp.attempts == []
    assert float(rig.store.get_meta("relay_template_alert:vship_update") or 0) == clock()


async def test_meta_out_of_reach_for_the_template_check_is_tried_again_later(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.guard.errors.append(WhatsAppSendError("could not reach WhatsApp to check template", retriable=True))
    with pytest.raises(WhatsAppSendError) as raised:
        await rig.run(item)
    assert raised.value.retriable
    # nothing decided, nothing done; only the verified command is kept, so its sender can always be told
    state = rig.state(item)
    assert state is not None and state["steps"] == {}
    pending = state["decision"]
    assert pending["action"] == "pending" and pending["employee"]["address"] == EMPLOYEE
    assert pending["phone"] == PHONE
    assert rig.notes() == [] and rig.whatsapp.attempts == []
    assert await rig.run(item) == 1  # decided anew
    assert rig.processor.status()["decisions"] == {"send": 1}


async def test_a_template_value_that_would_be_empty_refuses_the_update(tmp_path, clock):
    rig = Rig(tmp_path, clock, relay_template_params="name,message,booking")
    assert await rig.run(rig.email("WA | 650-555-1234")) == 0
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == "WhatsApp update NOT sent: a value the WhatsApp template needs is missing"
    assert "needs a booking number, but the email's subject has none" in text_of(notice)
    assert rig.whatsapp.attempts == []


async def test_dry_run_checks_everything_and_sends_nothing(tmp_path, clock):
    rig = Rig(tmp_path, clock, relay_dry_run=True)
    rig.customer_wrote()
    files = [
        (attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF-1.7"),
        (attachment("A2", "notes.txt", "text/plain"), b"hi"),
    ]
    item = rig.email(attachments=files)
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.whatsapp.uploads == [] and rig.mailbox.downloads == []
    [(_, notice)] = rig.notes()
    assert notice["X-WhatsApp-Relay"] == "dry_run"
    assert notice["Subject"] == f"DRY RUN - WhatsApp update not sent: {NUMBER}"
    text = text_of(notice)
    assert "Hello Maria, VShip has an update:" in text
    assert "Would be sent: Invoice.pdf (as its own WhatsApp message)" in text
    assert f"Would NOT be sent: notes.txt - {NOT_SUPPORTED}" in text
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    assert statuses(rig, item) == {"notice": "sent", "filed": "sent"}
    assert rig.guard.checked == ["vship_update"]


# ----------------------------------------------------------------------------- j.: attached files
async def test_inside_the_window_each_file_follows_the_update_as_its_own_message(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote(seconds_ago=3600)
    files = [
        (attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF-1.7 invoice"),
        (attachment("A2", "photo.JPG", "image/jpeg"), b"jpeg"),
        (attachment("A3", "clip.mp4", "video/mp4"), b"mp4"),
        (attachment("A4", "Packing list.docx", "application/octet-stream"), b"PK docx"),
    ]
    item = rig.email(attachments=files)
    assert await rig.run(item) == 5
    sent = rig.whatsapp.sent
    assert [m["type"] for m in sent] == ["template", "document", "image", "video", "document"]
    assert [(m["media_id"], m["filename"]) for m in sent[1:]] == [
        ("media1", "Invoice.pdf"),
        ("media2", None),
        ("media3", None),
        ("media4", "Packing list.docx"),
    ]
    assert all(m["phone"] == PHONE and m["number_id"] == NUMBER_ID for m in sent)
    assert rig.whatsapp.uploads == [
        ("Invoice.pdf", "application/pdf", b"%PDF-1.7 invoice"),
        ("photo.JPG", "image/jpeg", b"jpeg"),
        ("clip.mp4", "video/mp4", b"mp4"),
        ("Packing list.docx", DOCX, b"PK docx"),
    ]
    record = rig.store.get_relay_sent("wamid.OUT2")
    assert record is not None and record["what"] == "file:Invoice.pdf"
    text = text_of(rig.notes()[0][1])
    assert "Sent: Invoice.pdf (as its own WhatsApp message)" in text
    assert "Sent: Packing list.docx (as its own WhatsApp message)" in text
    assert spool_left(rig) == []


async def test_the_window_counts_only_recent_messages_from_that_customer_to_the_sending_number(
    tmp_path, clock
):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote(number_id="999999999999999")  # to another business number
    rig.customer_wrote(phone="16505559999")  # another customer
    rig.customer_wrote(seconds_ago=DAY - 300)  # 23 h 55 min ago: too late for a file
    item = rig.email(attachments=[(attachment("A1", "photo.jpg", "image/jpeg"), b"jpeg")])
    assert await rig.run(item) == 1
    assert rig.whatsapp.attempts == ["template"]
    assert f"NOT sent: photo.jpg - {NOT_PDF_OUTSIDE_WINDOW}" in text_of(rig.notes()[0][1])


async def test_outside_the_window_a_pdf_goes_with_the_document_template(tmp_path, clock):
    rig = Rig(
        tmp_path,
        clock,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    files = [
        (attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF"),
        (attachment("A2", "Packing list.docx", DOCX), b"PK"),
    ]
    item = rig.email("WA | 650-555-1234 | BK12345", attachments=files)
    assert await rig.run(item) == 2
    update, document = rig.whatsapp.sent
    assert update["name"] == "vship_update"
    assert document == {
        "type": "template",
        "number_id": NUMBER_ID,
        "phone": PHONE,
        "name": "vship_document",
        "language": "en_US",
        "body_params": ["Maria", "Invoice.pdf"],
        "header_document": {"id": "media1", "filename": "Invoice.pdf"},
        "wamid": "wamid.OUT2",
    }
    assert rig.whatsapp.uploads == [("Invoice.pdf", "application/pdf", b"%PDF")]
    text = text_of(rig.notes()[0][1])
    assert "Sent: Invoice.pdf (with the WhatsApp document template)" in text
    assert f"NOT sent: Packing list.docx - {NOT_PDF_OUTSIDE_WINDOW}" in text
    # checked when deciding and again right before each send (Meta's answers are kept by the real guard)
    assert rig.guard.checked == ["vship_update", "vship_document", "vship_update", "vship_document"]


async def test_outside_the_window_without_a_document_template_no_file_is_sent(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 1
    assert f"NOT sent: Invoice.pdf - {PDF_NO_TEMPLATE}" in text_of(rig.notes()[0][1])
    assert rig.mailbox.downloads == [] and rig.whatsapp.uploads == []


async def test_a_document_template_meta_does_not_allow_holds_back_the_pdf_and_alerts(tmp_path, clock):
    rig = Rig(
        tmp_path,
        clock,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    rig.guard.infos["vship_document"] = TemplateInfo(
        ok=False,
        status="PENDING",
        category="UTILITY",
        body_text=None,
        placeholders=0,
        problem="Its status at Meta is PENDING; only APPROVED templates can be sent.",
    )
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 1  # the update itself still goes
    notes = rig.notes()
    assert [n["X-WhatsApp-Relay"] for _, n in notes] == ["alert", "sent"]
    assert notes[0][1]["Subject"] == "[WhatsApp] ALERT: WhatsApp template vship_document cannot be used"
    assert (
        "the document template is not approved for use (Its status at Meta is PENDING; only APPROVED"
        " templates can be sent)." in text_of(notes[1][1])
    )
    assert rig.whatsapp.uploads == []


async def test_a_document_template_that_needs_a_booking_number_holds_back_the_pdf(tmp_path, clock):
    rig = Rig(
        tmp_path,
        clock,
        relay_document_template_name="vship_document",
        relay_document_template_params="booking,filename",
    )
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 1
    text = text_of(rig.notes()[0][1])
    assert "NOT sent: Invoice.pdf - The WhatsApp template vship_document needs a booking number" in text


async def test_pictures_in_the_text_attached_emails_and_cloud_links_are_never_sent(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    files = [
        (attachment("A1", "image001.png", "image/png", inline=True, content_id=LOGO_CID), b"logo"),
        # not marked inline by Graph, but shown in the text: a pasted picture as well
        (attachment("A2", "Screenshot.png", "image/png", content_id="<screenshot@01DC5A11>"), b"shot"),
        (attachment("A3", "Booking confirmation", "message/rfc822", kind="item"), b""),
        (attachment("A4", "Contract.docx", DOCX, kind="reference"), b""),
        (attachment("A5", "big.png", "image/png", size=5_000_001), b"big"),
        (attachment("A6", "notes.txt", "text/plain"), b"notes"),
    ]
    html = outlook(*LINES, extra='<p class="MsoNormal"><img src="cid:screenshot@01DC5A11"><o:p></o:p></p>')
    item = rig.email(html=html, attachments=files)
    assert await rig.run(item) == 1
    assert rig.whatsapp.attempts == ["template"] and rig.mailbox.downloads == []
    text = text_of(rig.notes()[0][1])
    assert f"NOT sent: Booking confirmation - {NOT_A_FILE}" in text
    assert f"NOT sent: Contract.docx - {NOT_A_FILE}" in text
    assert "NOT sent: big.png - WhatsApp takes at most 5 MB for pictures." in text
    assert f"NOT sent: notes.txt - {NOT_SUPPORTED}" in text
    assert "image001.png" not in text and "Screenshot.png" not in text
    assert PASTED_PICTURES_NOTE in text


async def test_files_are_never_sent_when_the_update_was_not(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.whatsapp.fail(
        "template",
        WhatsAppSendError("WhatsApp send returned HTTP 400: undeliverable", code=131026, status_code=400),
    )
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == [] and rig.whatsapp.uploads == [] and rig.mailbox.downloads == []
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == "WhatsApp update NOT sent: WhatsApp refused it"
    text = text_of(notice)
    assert "WhatsApp could not deliver the message to this customer" in text  # Meta's code in words
    assert "WhatsApp error code: 131026" in text
    assert "NOT sent: Invoice.pdf - the update itself was not sent" in text
    assert statuses(rig, item) == {
        "template": "refused",
        "file:A1": "skipped",
        "notice": "sent",
        "filed": "sent",
    }


async def test_a_file_whatsapp_refuses_does_not_stop_the_others(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.whatsapp.upload_errors.append(
        WhatsAppSendError("WhatsApp file upload returned HTTP 400: bad file", code=131053, status_code=400)
    )
    files = [
        (attachment("A1", "broken.pdf", "application/pdf"), b"%PDF broken"),
        (attachment("A2", "photo.png", "image/png"), b"png"),
    ]
    item = rig.email(attachments=files)
    assert await rig.run(item) == 2
    assert [m["type"] for m in rig.whatsapp.sent] == ["template", "image"]
    text = text_of(rig.notes()[0][1])
    assert "NOT sent: broken.pdf - WhatsApp could not accept the file" in text
    assert "Sent: photo.png (as its own WhatsApp message)" in text


async def test_the_spool_is_cleaned_and_a_retry_sends_only_the_missing_files(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    files = [
        (attachment("A1", "a.pdf", "application/pdf"), b"%PDF a"),
        (attachment("A2", "b.pdf", "application/pdf"), b"%PDF b"),
    ]
    item = rig.email(attachments=files)
    rig.mailbox.fail("download_attachment", None, SendError("Graph download attachment returned HTTP 503"))
    with pytest.raises(SendError):
        await rig.run(item)
    assert [m["type"] for m in rig.whatsapp.sent] == ["template", "document"]
    assert spool_left(rig) == []
    assert rig.notes() == []  # the employee hears once everything is done
    assert await rig.run(item) == 3
    assert [m.get("filename") for m in rig.whatsapp.sent] == [None, "a.pdf", "b.pdf"]
    assert len(rig.notes()) == 1 and spool_left(rig) == []


# ----------------------------------------------------------------------------- k.: the CRM
@pytest.mark.parametrize(
    ("lookup", "line"),
    [
        (matched(), "VShipCRM: Customer #42: Acme Imports / John Doe"),
        (
            LookupResult(
                LookupStatus.ERROR, reason="CRM unavailable and no cached customer list yet: timeout"
            ),
            "VShipCRM: CRM lookup failed: CRM unavailable and no cached customer list yet: timeout",
        ),
        (
            LookupResult(LookupStatus.DISABLED, reason="CRM lookup is disabled"),
            "VShipCRM: CRM lookup is switched off",
        ),
        (LookupResult(LookupStatus.NO_PHONE), "VShipCRM: the number could not be looked up"),
    ],
)
async def test_the_crm_match_is_reported_and_never_blocks_sending(tmp_path, clock, lookup, line):
    rig = Rig(tmp_path, clock)
    rig.directory.result = lookup
    assert await rig.run(rig.email()) == 1
    assert line in text_of(rig.notes()[0][1])


async def test_a_crashing_crm_lookup_does_not_stop_the_update(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.directory.error = RuntimeError("index broken")
    assert await rig.run(rig.email()) == 1
    assert "VShipCRM: CRM lookup failed: RuntimeError" in text_of(rig.notes()[0][1])
    rig.processor.directory = None  # no CRM at all
    assert await rig.run(rig.email()) == 1
    assert "VShipCRM: CRM lookup is switched off" in text_of(rig.notes()[1][1])


def test_crm_match_text_for_a_customer_without_an_email_address():
    result = matched()
    result.status, result.reply_to = LookupStatus.NO_EMAIL, []
    assert crm_match_text(result) == "Customer #42: Acme Imports / John Doe"
    assert crm_match_text(LookupResult(LookupStatus.NO_MATCH)) == "no CRM customer has this number"
    assert crm_match_text(LookupResult(LookupStatus.ERROR)) == "CRM lookup failed: no reason given"


# ----------------------------------------------------------------------------- WhatsApp's answers
async def test_an_unclear_answer_is_never_retried(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.whatsapp.fail("template", WhatsAppSendError("no answer from WhatsApp: ReadTimeout", uncertain=True))
    item = rig.email(attachments=[(attachment("A1", "a.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 0
    [(_, notice)] = rig.notes()
    assert notice["X-WhatsApp-Relay"] == "uncertain"
    assert notice["Subject"] == f"WhatsApp update MAY NOT have been sent: {NUMBER}"
    text = text_of(notice)
    assert "WhatsApp did not confirm that it took the message" in text
    assert "NOT sent: a.pdf - the update itself may not have been sent" in text
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    rig.restart()
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == ["template"]  # tried once only, ever
    assert rig.whatsapp.uploads == []


async def test_a_plain_not_taken_answer_is_tried_again_and_sends_once(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    error = WhatsAppSendError(
        "WhatsApp send returned HTTP 429: rate limit hit",
        code=130429,
        retriable=True,
        status_code=429,
        retry_after=30.0,
    )
    rig.whatsapp.fail("template", error)
    item = rig.email()
    with pytest.raises(WhatsAppSendError) as raised:
        await rig.run(item)
    assert raised.value is error and raised.value.retry_after == 30.0  # for the worker's retry delay
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "send" and state["steps"] == {}
    assert rig.notes() == [] and rig.mailbox.moved == []
    clock.advance(30)
    assert await rig.run(item) == 1
    assert len(rig.whatsapp.sent) == 1 and rig.whatsapp.attempts == ["template", "template"]
    assert len(rig.notes()) == 1


async def test_a_template_error_about_the_values_drops_the_kept_check_without_an_alert(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.whatsapp.fail("template", WhatsAppSendError("text too long", code=132005, status_code=400))
    await rig.run(rig.email())
    assert rig.guard.forgotten == ["vship_update"]
    [(_, notice)] = rig.notes()  # nothing for ALERT_TO: the employee can fix this one
    assert notice["Subject"] == "WhatsApp update NOT sent: WhatsApp refused it"
    text = text_of(notice)
    assert "The text is too long for the WhatsApp template" in text and "WhatsApp error code: 132005" in text


@pytest.mark.parametrize(
    ("code", "words"),
    [
        (132000, "did not match the number of variables"),
        (132001, "does not exist in that language, or Meta has not approved it"),
        (132015, "Meta has paused the WhatsApp template because of low quality"),
        (132016, "Meta has permanently disabled the WhatsApp template"),
    ],
)
async def test_a_template_meta_stopped_after_the_last_check_refuses_the_update_and_alerts(
    tmp_path, clock, code, words
):
    """Meta can pause or withdraw a template while the guard still keeps its earlier APPROVED answer."""
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.whatsapp.fail(
        "template", WhatsAppSendError(f"WhatsApp send returned HTTP 400 ({code})", code=code, status_code=400)
    )
    item = rig.email(attachments=[(attachment("A1", "a.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 0
    assert rig.guard.forgotten == ["vship_update"]  # the next email asks Meta again
    [(alert_key, alert), (_, notice)] = rig.notes()
    assert alert_key.startswith("note:relay_alert:template:vship_update:")
    assert alert["To"] == ALERT_TO
    assert alert["Subject"] == "[WhatsApp] ALERT: WhatsApp template vship_update cannot be used"
    text = text_of(alert)
    assert f"Meta refused to send a message with WhatsApp template vship_update (error code {code})" in text
    assert words in text
    assert notice["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert notice["Subject"] == "WhatsApp update NOT sent: the WhatsApp template is not ready"
    text = text_of(notice)
    assert "Reason: WhatsApp refused to send your update." in text
    assert words in text and f"WhatsApp error code: {code}" in text
    assert "an administrator has been alerted" in text
    assert "NOT sent: a.pdf - the update itself was not sent" in text
    assert statuses(rig, item) == {
        "template": "refused",
        "file:A1": "skipped",
        "alert": "sent",
        "notice": "sent",
        "filed": "sent",
    }
    assert rig.whatsapp.uploads == []


async def test_a_template_meta_paused_is_alerted_at_most_once_a_day(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    for _ in range(2):
        rig.whatsapp.fail("template", WhatsAppSendError("paused", code=132015, status_code=400))
        assert await rig.run(rig.email()) == 0
        clock.advance(3600)
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()] == ["alert", "refused", "refused"]
    clock.advance(DAY)
    rig.whatsapp.fail("template", WhatsAppSendError("paused", code=132015, status_code=400))
    await rig.run(rig.email())
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()][3:] == ["alert", "refused"]


async def test_a_document_template_meta_stopped_holds_back_the_pdf_and_alerts(tmp_path, clock):
    rig = Rig(
        tmp_path,
        clock,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    send_template = rig.whatsapp.send_template

    async def document_template_disabled(number_id, *, name, **rest):
        if name == "vship_document":
            rig.whatsapp.attempts.append("template")
            raise WhatsAppSendError("WhatsApp send returned HTTP 400", code=132016, status_code=400)
        return await send_template(number_id, name=name, **rest)

    rig.whatsapp.send_template = document_template_disabled  # type: ignore[method-assign]
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    assert await rig.run(item) == 1  # the update itself went out
    assert rig.guard.forgotten == ["vship_document"]
    [(_, alert), (_, notice)] = rig.notes()
    assert alert["Subject"] == "[WhatsApp] ALERT: WhatsApp template vship_document cannot be used"
    assert "WhatsApp template vship_document (error code 132016)" in text_of(alert)
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER}"
    assert "NOT sent: Invoice.pdf - Meta has permanently disabled the WhatsApp template" in text_of(notice)


async def test_the_pair_rate_limit_on_a_file_retries_only_the_file(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    error = WhatsAppSendError(
        "WhatsApp send returned HTTP 400: pair rate limit", code=131056, retriable=True, retry_after=10.0
    )
    rig.whatsapp.fail("document", error)
    item = rig.email(attachments=[(attachment("A1", "a.pdf", "application/pdf"), b"%PDF")])
    with pytest.raises(WhatsAppSendError) as raised:
        await rig.run(item)
    assert raised.value.retry_after == 10.0
    assert statuses(rig, item) == {"template": "sent"}
    assert spool_left(rig) == []
    clock.advance(10)
    assert await rig.run(item) == 2
    assert [m["type"] for m in rig.whatsapp.sent] == ["template", "document"]
    assert len(rig.whatsapp.uploads) == 2  # uploaded again right before the send, which is harmless
    assert len(rig.notes()) == 1


# ----------------------------------------------------------------------------- never twice
async def test_a_crash_right_after_the_update_went_out_never_sends_it_again(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.whatsapp.crash_after["template"] = Crash("power cut")
    item = rig.email()
    with pytest.raises(Crash):
        await rig.run(item)
    assert len(rig.whatsapp.sent) == 1  # it did reach WhatsApp ...
    assert statuses(rig, item) == {"template": "sending"}  # ... but the service never learned it
    rig.restart()
    assert await rig.run(item) == 0
    assert len(rig.whatsapp.attempts) == 1
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == f"WhatsApp update MAY NOT have been sent: {NUMBER}"
    assert INTERRUPTED in text_of(notice)
    assert statuses(rig, item) == {"template": "uncertain", "notice": "sent", "filed": "sent"}


async def test_a_failed_state_write_after_the_send_never_sends_it_again(tmp_path, clock, monkeypatch):
    rig = Rig(tmp_path, clock)
    save = rig.store.save_relay_state
    broken: list[str] = []

    def flaky(key: str, state: dict[str, Any], now: float | None = None) -> None:
        if (state["steps"].get("template") or {}).get("status") == "sent" and not broken:
            broken.append(key)
            raise sqlite3.OperationalError("disk I/O error")
        save(key, state, now)

    monkeypatch.setattr(rig.store, "save_relay_state", flaky)
    item = rig.email()
    with pytest.raises(sqlite3.OperationalError):
        await rig.run(item)
    rig.restart()
    assert await rig.run(item) == 0
    assert len(rig.whatsapp.attempts) == 1
    assert rig.notes()[0][1]["X-WhatsApp-Relay"] == "uncertain"
    # recorded before the state, so a later "failed" report still reaches the employee
    record = rig.store.get_relay_sent("wamid.OUT1")
    assert record is not None and record["item_key"] == item.key


async def test_a_crash_after_a_file_went_out_never_sends_that_file_again(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.whatsapp.crash_after["document"] = Crash("killed")
    files = [
        (attachment("A1", "a.pdf", "application/pdf"), b"a"),
        (attachment("A2", "b.jpg", "image/jpeg"), b"b"),
    ]
    item = rig.email(attachments=files)
    with pytest.raises(Crash):
        await rig.run(item)
    assert spool_left(rig) == []  # cleaned on the way out
    rig.restart()
    assert await rig.run(item) == 2  # the update and the picture; the PDF may or may not have arrived
    assert rig.whatsapp.attempts == ["template", "document", "image"]
    text = text_of(rig.notes()[0][1])
    assert f"MAY NOT have been sent: a.pdf - {INTERRUPTED}" in text
    assert "Sent: b.jpg (as its own WhatsApp message)" in text


async def test_filing_is_tried_again_without_a_second_notice(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.mailbox.fail("move", SendError("Graph move message returned HTTP 503", status_code=503))
    with pytest.raises(SendError):
        await rig.run(item)
    assert len(rig.notes()) == 1 and rig.mailbox.moved == []
    assert await rig.run(item) == 1
    assert len(rig.notes()) == 1 and len(rig.whatsapp.attempts) == 1
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


async def test_a_crash_while_queueing_the_notice_still_tells_the_employee_exactly_once(
    tmp_path, clock, monkeypatch
):
    rig = Rig(tmp_path, clock)
    save = rig.store.save_relay_state
    broken: list[str] = []

    def flaky(key: str, state: dict[str, Any], now: float | None = None) -> None:
        if (state["steps"].get("notice") or {}).get("status") == "sent" and not broken:
            broken.append(key)
            raise sqlite3.OperationalError("database is locked")
        save(key, state, now)

    monkeypatch.setattr(rig.store, "save_relay_state", flaky)
    item = rig.email()
    with pytest.raises(sqlite3.OperationalError):
        await rig.run(item)
    assert len(rig.notes()) == 1
    rig.restart()
    assert await rig.run(item) == 1
    assert len(rig.notes()) == 1 and len(rig.whatsapp.attempts) == 1
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


async def test_a_dead_lettered_email_is_never_sent_by_a_replay(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    for _ in range(3):
        rig.whatsapp.fail(
            "template", WhatsAppSendError("could not reach WhatsApp: ConnectError", retriable=True)
        )
        with pytest.raises(WhatsAppSendError):
            await rig.run(item)
        clock.advance(120)
    error = "gave up after 3 attempt(s): could not reach WhatsApp: ConnectError"
    rig.store.mark_failed(item.id, error, clock())  # what the worker does ...
    failed = rig.store.get(item.key)
    assert failed is not None
    rig.processor.on_dead_letter(failed, error)  # ... and then tells the processor
    notes = rig.notes()
    assert notes[0][0] == f"note:relay:{rig.graph_id(item)}:result"  # the notice step's own key
    assert notes[1][0].startswith("note:relay_alert:dead:")
    employee = notes[0][1]
    assert employee["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert employee["Subject"] == "WhatsApp update NOT sent: WhatsApp could not be reached in time"
    assert "It will not be sent later on its own." in text_of(employee)
    alert = text_of(notes[1][1])
    assert notes[1][1]["To"] == ALERT_TO
    assert f"Error: {error}" in alert and f"From: {EMPLOYEE}" in alert
    assert "The update: NOT sent." in alert
    assert "The employee has been told what did and did not go out." in alert
    # an administrator replays it once WhatsApp works again: only the bookkeeping is finished
    assert rig.store.requeue(item.key, clock()) == 1
    rig.restart()
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == [] and len(rig.whatsapp.attempts) == 3
    assert len(rig.notes()) == 2  # no second email to the employee
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


async def test_a_dead_letter_after_the_update_went_out_reports_the_files_that_did_not(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    item = rig.email(attachments=[(attachment("A1", "a.pdf", "application/pdf"), b"%PDF")])
    rig.mailbox.fail("download_attachment", SendError("Graph download attachment returned HTTP 503"))
    with pytest.raises(SendError):
        await rig.run(item)
    error = "gave up after 4 attempt(s): Graph download attachment returned HTTP 503"
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, error)
    [(_, notice), (_, alert)] = rig.notes()
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER}"
    assert f"NOT sent: a.pdf - {GAVE_UP_FILE}" in text_of(notice)
    assert "The update: sent." in text_of(alert) and "Files: 1 NOT sent." in text_of(alert)
    assert await rig.run(item) == 1  # replayed: nothing more goes out
    assert len(rig.whatsapp.attempts) == 1 and rig.mailbox.downloads == []
    assert len(rig.notes()) == 2


async def test_a_dead_letter_before_the_email_was_read_alerts_the_administrators_only(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.mailbox.fail("get_message", SendError("Graph get message returned HTTP 503"))
    with pytest.raises(SendError):
        await rig.run(item)
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 5 attempt(s): Graph get message returned HTTP 503")
    [(key, alert)] = rig.notes()
    assert key.startswith("note:relay_alert:dead:") and alert["To"] == ALERT_TO
    assert "its sender has not been told" in text_of(alert)
    assert await rig.run(item) == 1  # a replay handles it as usual


async def test_a_dead_letter_after_the_outcome_was_told_only_alerts(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.mailbox.fail("move", *[SendError("Graph move message returned HTTP 503")] * 3)
    for _ in range(3):
        with pytest.raises(SendError):
            await rig.run(item)
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 3 attempt(s): Graph move message returned HTTP 503")
    [(_, notice), (_, alert)] = rig.notes()
    assert notice["X-WhatsApp-Relay"] == "sent"
    assert "The employee had already been told the outcome." in text_of(alert)


async def test_an_outcome_queued_just_before_a_failed_save_is_not_sent_again_by_the_dead_letter(
    tmp_path, clock, monkeypatch
):
    rig = Rig(tmp_path, clock)
    save = rig.store.save_relay_state
    broken: list[str] = []

    def flaky(key: str, state: dict[str, Any], now: float | None = None) -> None:
        if (state["steps"].get("notice") or {}).get("status") == "sent" and not broken:
            broken.append(key)
            raise sqlite3.OperationalError("database is locked")
        save(key, state, now)

    monkeypatch.setattr(rig.store, "save_relay_state", flaky)
    item = rig.email()
    with pytest.raises(sqlite3.OperationalError):
        await rig.run(item)
    assert statuses(rig, item)["notice"] == "sending"  # queued, but the state could not say so
    # the worker gives up before the next attempt (its retry window is used up, or the crash cap)
    rig.store.mark_failed(item.id, "gave up after 3 attempt(s): database is locked", clock())
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 3 attempt(s): database is locked")
    [(key, notice), (_, alert)] = rig.notes()  # one outcome email: not a second "sent" email
    assert key == f"note:relay:{rig.graph_id(item)}:result"
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER}"
    assert "The employee had already been told the outcome." in text_of(alert)
    assert rig.store.requeue(item.key, clock()) == 1
    assert await rig.run(item) == 1  # replayed: filed, nobody told again, nothing sent again
    assert len(rig.notes()) == 2 and len(rig.whatsapp.attempts) == 1
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


async def test_a_dead_letter_whose_last_save_fails_still_counts_the_employee_as_told(
    tmp_path, clock, monkeypatch
):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    save = rig.store.save_relay_state

    def flaky(key: str, state: dict[str, Any], now: float | None = None) -> None:
        if (state["steps"].get("notice") or {}).get("note") == "dead":
            raise sqlite3.OperationalError("database is locked")
        save(key, state, now)

    monkeypatch.setattr(rig.store, "save_relay_state", flaky)
    rig.store.mark_failed(item.id, "gave up", clock())
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 4 attempt(s): could not reach WhatsApp")
    monkeypatch.setattr(rig.store, "save_relay_state", save)
    [(key, notice), (_, alert)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result"
    assert notice["Subject"] == "WhatsApp update NOT sent: WhatsApp could not be reached in time"
    text = text_of(alert)
    assert "The employee has been told what did and did not go out." in text
    assert "Nothing more will be sent for this email." in text
    assert rig.store.requeue(item.key, clock()) == 1
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == [] and len(rig.notes()) == 2  # no second "NOT sent" email
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


async def test_a_dead_letter_that_cannot_save_its_state_still_tells_everyone_and_a_replay_sends_nothing(
    tmp_path, clock, monkeypatch
):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp: ConnectError", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    rig.store.mark_failed(item.id, "gave up", clock())  # the worker's own record is written first
    save = rig.store.save_relay_state

    def locked(key: str, state: dict[str, Any], now: float | None = None) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.store, "save_relay_state", locked)
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 4 attempt(s): ConnectError")
    monkeypatch.setattr(rig.store, "save_relay_state", save)
    [(key, notice), (alert_key, alert)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result" and alert_key.startswith("note:relay_alert:dead:")
    assert notice["Subject"] == "WhatsApp update NOT sent: WhatsApp could not be reached in time"
    assert "Nothing more will be sent for this email." in text_of(alert)
    # replayed the same day, with WhatsApp working: the employee was told "NOT sent", so nothing goes out
    assert rig.store.requeue(item.key, clock()) == 1
    rig.restart()
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == [] and len(rig.whatsapp.attempts) == 1
    assert len(rig.notes()) == 2
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]
    assert statuses(rig, item)["template"] == "skipped"


async def test_a_dead_letter_that_could_record_nothing_says_not_to_replay(tmp_path, clock, monkeypatch):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    enqueue = rig.store.enqueue

    def locked(key: str, state: dict[str, Any], now: float | None = None) -> None:
        raise sqlite3.OperationalError("database is locked")

    def only_alerts(kind: str, key: str, payload: dict[str, Any], now: float | None = None) -> bool:
        if key.startswith("note:relay:"):
            raise sqlite3.OperationalError("database is locked")
        return enqueue(kind, key, payload, now)

    monkeypatch.setattr(rig.store, "save_relay_state", locked)
    monkeypatch.setattr(rig.store, "enqueue", only_alerts)
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 4 attempt(s): could not reach WhatsApp")
    [(_, alert)] = rig.notes()
    text = text_of(alert)
    assert "The employee could NOT be told (see the service log); please tell them." in text
    assert "Do NOT replay it (whatsapp-webhook replay-failed): a replay could still send the update." in text
    assert "Nothing more will be sent" not in text


async def test_reporting_the_same_dead_letter_twice_queues_each_email_once(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.email()
    claimed = rig.store.claim_next(clock())  # as the worker holds it, with the time of its first attempt
    assert claimed is not None
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.processor.process(claimed)
    for _ in range(2):  # for example when the worker has to repeat its own dead-letter bookkeeping
        rig.processor.on_dead_letter(claimed, "gave up after 4 attempt(s): could not reach WhatsApp")
        clock.advance(2)
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()] == ["refused", "alert"]


async def test_a_database_locked_right_after_the_worker_gives_up_hides_nothing(tmp_path, clock, monkeypatch):
    """The worker tells the processor before it records the dead letter. A database that an operator's command
    locks from that moment on, for longer than any wait, cannot keep the employee and the administrators
    from hearing of the update, nor let a replay send what the employee was told was not sent."""
    rig = Rig(tmp_path, clock)
    worker = Worker(
        rig.settings,
        rig.store,
        rig.directory,
        FakeTransport(),
        httpx.AsyncClient(),
        clock=clock,
        relay=rig.processor,
    )
    rig.processor.wakeup = worker.wakeup
    rig.whatsapp.fail("template", *[WhatsAppSendError("could not reach WhatsApp", retriable=True)] * 20)
    item = rig.email()
    store = rig.store
    mark_failed = store.mark_failed
    locked: list[bool] = []

    def failed_then_locked(item_id: int, error: str, now: float | None = None) -> None:
        mark_failed(item_id, error, now)
        locked.append(True)

    def unless_locked(write: Any) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if locked:
                raise sqlite3.OperationalError("database is locked")
            return write(*args, **kwargs)

        return guarded

    monkeypatch.setattr(store, "mark_failed", failed_then_locked)
    for name in ("save_relay_state", "enqueue", "set_meta", "mark_retry"):
        monkeypatch.setattr(store, name, unless_locked(getattr(store, name)))
    for _ in range(50):  # the worker's loop, with the clock jumping to each retry
        current = store.claim_next(clock())
        if current is None:
            clock.now = store.next_due_at() or clock.now
            continue
        await worker.handle(current)
        if store.get(item.key).status == STATUS_FAILED:
            break
    assert locked and store.get(item.key).status == STATUS_FAILED
    locked.clear()
    [(key, notice), (alert_key, alert)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result" and alert_key.startswith("note:relay_alert:dead:")
    assert notice["Subject"] == "WhatsApp update NOT sent: WhatsApp could not be reached in time"
    assert "Nothing more will be sent for this email." in text_of(alert)
    # replayed once WhatsApp works again: nothing goes out, nobody is told twice
    rig.whatsapp.failures.clear()
    attempts = len(rig.whatsapp.attempts)
    assert store.requeue(item.key, clock()) == 1
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == [] and len(rig.whatsapp.attempts) == attempts
    assert len(rig.notes()) == 2 and rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


@pytest.mark.parametrize("failing", ["template check", "attachment list", "document template check"])
async def test_given_up_after_the_sender_was_verified_tells_the_employee_and_never_sends_later(
    tmp_path, clock, failing
):
    rig = Rig(
        tmp_path,
        clock,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    item = rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")])
    check = rig.guard.check
    error: Exception
    if failing == "template check":
        error = WhatsAppSendError("WhatsApp template list returned HTTP 503", retriable=True)
        rig.guard.errors.append(error)
    elif failing == "attachment list":
        error = SendError("Graph list attachments returned HTTP 503", status_code=503)
        rig.mailbox.fail("list_attachments", error)
    else:
        error = WhatsAppSendError("WhatsApp template list returned HTTP 503", retriable=True)

        async def document_check_fails(spec: TemplateSpec) -> TemplateInfo:
            if spec.name == "vship_document":
                raise error
            return await check(spec)

        rig.guard.check = document_check_fails  # type: ignore[method-assign]
    with pytest.raises(type(error)):
        await rig.run(item)
    state = rig.state(item)
    assert state is not None and state["decision"]["action"] == "pending"
    rig.store.mark_failed(item.id, "gave up", clock())  # every attempt of the retry window failed alike
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, f"gave up after 8 attempt(s): {error}")
    [(key, notice), (_, alert)] = rig.notes()
    assert key == f"note:relay:{rig.graph_id(item)}:result"
    assert notice["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert notice["Subject"] == f"WhatsApp update NOT sent: {GAVE_UP_SHORT}"
    text = text_of(notice)
    assert "It will not be sent later on its own." in text
    assert "send it again as a new email in a few minutes" in text
    assert f"Phone number: {NUMBER}" in text
    text = text_of(alert)
    assert f"From: {EMPLOYEE}" in text and "had not been read yet" not in text
    assert "The update: NOT sent (the service gave up while it was still checking the email)." in text
    assert "The employee has been told what did and did not go out." in text
    assert "Nothing more will be sent for this email." in text
    # replayed once Meta and the mailbox work again: the email is filed, nothing is sent, nobody is told again
    rig.guard.check = check  # type: ignore[method-assign]
    assert rig.store.requeue(item.key, clock()) == 1
    rig.restart()
    assert await rig.run(item) == 0
    assert rig.whatsapp.attempts == [] and rig.whatsapp.uploads == []
    assert len(rig.notes()) == 2
    assert rig.mailbox.moved == [(rig.graph_id(item), FOLDER)]


@pytest.mark.parametrize(
    "saved",
    [
        "{broken",
        json.dumps({"decision": {"action": "send", "employee": {"address": EMPLOYEE}}, "steps": ["x"]}),
    ],
)
async def test_an_unreadable_state_is_never_taken_for_a_fresh_start(tmp_path, clock, saved):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    conn = sqlite3.connect(rig.settings.store_path)  # as an operator's sqlite3 shell would
    try:
        conn.execute(
            "INSERT INTO relay_state (key, state, updated_at) VALUES (?, ?, ?)", (item.key, saved, 1.0)
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        await rig.run(item)
    assert rig.whatsapp.attempts == [] and rig.mailbox.calls == []
    current = rig.store.get(item.key)
    assert current is not None
    rig.processor.on_dead_letter(current, "gave up after 3 attempt(s): ValueError")
    [(_, alert)] = rig.notes()
    assert "Its saved state cannot be read, so the service cannot tell what was sent. Do not replay it" in (
        text_of(alert)
    )


async def test_on_dead_letter_flush_and_status_never_raise(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    await rig.run(rig.email(author="mallory@vship2000.com"))
    rig.store.close()  # the database is gone
    rig.processor.on_dead_letter(item, "boom")
    clock.advance(2 * DAY)
    rig.processor.flush_digests()
    status = rig.processor.status()
    assert status["decisions"] == {"not_allowed": 1}
    assert status["not_allowed_waiting"] is None


# ----------------------------------------------------------------------------- delivery reports
async def test_a_failed_delivery_report_tells_the_employee_once(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email("WA | 650-555-1234 | BK12345")
    await rig.run(item)
    clock.advance(30)
    report = rig.status_item("wamid.OUT1", code=131026, title="Message undeliverable")
    await rig.processor.process_status(report)
    await rig.processor.process_status(report)  # handled again (a retry): still one email
    await rig.processor.process_status(rig.status_item("wamid.OUT1", code=131026))  # Meta reports it twice
    notes = rig.notes()
    assert len(notes) == 2
    key, failed = notes[1]
    assert key == "note:relay_status:wamid.OUT1"
    assert failed["X-WhatsApp-Relay"] == "delivery_failed"
    assert failed["Subject"] == f"WhatsApp could not deliver your update to {NUMBER}"
    assert failed["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert failed["In-Reply-To"] == notes[0][1]["In-Reply-To"]  # in the conversation of the command
    text = text_of(failed)
    assert "WhatsApp could not deliver the message to this customer" in text
    assert "WhatsApp error code: 131026" in text
    assert "Your WhatsApp update (template vship_update)" in text
    record = rig.store.get_relay_sent("wamid.OUT1")
    assert record is not None and record["failure_notified"] is True
    assert rig.wakeup.is_set()


async def test_a_failed_file_report_names_the_file(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    await rig.run(rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")]))
    await rig.processor.process_status(
        rig.status_item("wamid.OUT2", code=131047, title="Re-engagement message")
    )
    _, failed = rig.notes()[-1]
    text = text_of(failed)
    assert f"WhatsApp could not deliver the file Invoice.pdf to {NUMBER}." in text
    assert "More than 24 hours had passed since the customer's last WhatsApp message" in text


async def test_other_reports_and_messages_the_feature_did_not_send_are_ignored(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    await rig.run(rig.email())
    await rig.processor.process_status(rig.status_item("wamid.FROM_THE_FORWARDER", code=131026))
    await rig.processor.process_status(rig.status_item("wamid.OUT1", status="delivered", code=None))
    await rig.processor.process_status(rig.status_item("", code=131026))
    assert len(rig.notes()) == 1
    record = rig.store.get_relay_sent("wamid.OUT1")
    assert record is not None and record["failure_notified"] is False


# ----------------------------------------------------------------------------- rules checked again later
@pytest.mark.parametrize(
    ("changes", "subject"),
    [
        ({"relay_dry_run": True}, "the service was switched to dry run"),
        ({"relay_test_numbers": "+1 650 555 0000"}, "test mode is on (not a test number)"),
        ({"relay_allowed_senders": "ops@vship2000.com"}, "your address may no longer send updates"),
    ],
)
async def test_a_restart_with_stricter_settings_stops_a_pending_send(tmp_path, clock, changes, subject):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    rig.restart(**changes)
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == []
    [(_, notice)] = rig.notes()
    assert notice["Subject"] == f"WhatsApp update NOT sent: {subject}"


async def test_an_update_that_became_too_old_while_waiting_is_not_sent(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email(age=23 * 3600)
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    clock.advance(2 * 3600)
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == []
    assert rig.notes()[0][1]["Subject"] == "WhatsApp update NOT sent: your email is too old"


async def test_a_template_paused_before_the_retry_is_not_sent(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    item = rig.email()
    rig.whatsapp.fail("template", WhatsAppSendError("could not reach WhatsApp", retriable=True))
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    rig.guard.infos["vship_update"] = TemplateInfo(
        ok=False,
        status="PAUSED",
        category="UTILITY",
        body_text=UPDATE_BODY,
        placeholders=2,
        problem="It is PAUSED.",
    )
    assert await rig.run(item) == 0
    assert rig.whatsapp.sent == []
    assert [n["X-WhatsApp-Relay"] for _, n in rig.notes()] == ["alert", "refused"]


async def test_a_window_that_closed_before_a_retried_file_holds_the_file_back(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    rig.customer_wrote(seconds_ago=DAY - 900)  # 15 minutes left of the window, of which 10 are kept free
    rig.whatsapp.fail("image", WhatsAppSendError("pair rate limit", code=131056, retriable=True))
    item = rig.email(attachments=[(attachment("A1", "photo.png", "image/png"), b"png")])
    with pytest.raises(WhatsAppSendError):
        await rig.run(item)
    clock.advance(600)
    assert await rig.run(item) == 1
    assert rig.whatsapp.attempts == ["template", "image"]
    assert "NOT sent: photo.png - The customer's 24-hour WhatsApp window closed" in text_of(rig.notes()[0][1])


# ----------------------------------------------------------------------------- the rest
async def test_an_item_without_an_email_id_is_a_permanent_failure(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    assert rig.store.enqueue(KIND_RELAY, "relay:broken", {"received": "2023-11-14T22:10:00Z"}, clock())
    item = rig.store.get("relay:broken")
    assert item is not None
    with pytest.raises(Exception) as raised:
        await rig.processor.process(item)
    assert type(raised.value).__name__ == "PermanentFailure"


async def test_status_is_a_small_summary(tmp_path, clock):
    rig = Rig(tmp_path, clock)
    await rig.run(rig.email())
    await rig.run(rig.email("RE: WA | 650-555-1234"))
    status = rig.processor.status()
    assert status == {
        "decisions": {"send": 1, "ignore": 1},
        "last_decision_at": clock(),
        "messages_sent": 1,
        "last_sent_at": clock(),
        "not_allowed_waiting": 0,
    }


async def test_logs_hold_neither_the_message_nor_the_full_phone_number(tmp_path, clock, caplog):
    caplog.set_level(logging.DEBUG)
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    rig.directory.error = RuntimeError("lookup of +16505551234 failed")
    await rig.run(rig.email(attachments=[(attachment("A1", "Invoice.pdf", "application/pdf"), b"%PDF")]))
    await rig.run(rig.email(lines=("Name:Maria", "Message:", "##")))
    rig.whatsapp.fail("template", WhatsAppSendError("no answer", uncertain=True))
    await rig.run(rig.email())
    await rig.run(rig.email(author="mallory@vship2000.com"))
    await rig.processor.process_status(rig.status_item("wamid.OUT1", code=131026))
    logged = caplog.text
    assert "6505551234" not in logged and "650-555-1234" not in logged and "555-1234" not in logged
    assert "Maria" not in logged and "Newark" not in logged
    assert EMPLOYEE in logged and "mallory@vship2000.com" in logged  # sender addresses may be logged
    assert "+16*****1234" in logged  # app.worker.mask_phone
    assert "file:A1 sent" in logged


# ----------------------------------------------------------------------------- end to end: Graph and Meta
GRAPH = "https://graph.microsoft.com/v1.0"
BOX = f"{GRAPH}/users/{MAILBOX}"
TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
META = "https://graph.facebook.com/v27.0"
E2E_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZgABAAAAs7rpJGO0vUe1LJPC9Ukz8AAAAqDLSAAA="
E2E_URL = f"{BOX}/messages/{quote(E2E_ID, safe='')}"
E2E_INTERNET_ID = "<SA1PR11MB8476E2E0A1B2C3D4E5F6@SA1PR11MB8476.namprd11.prod.outlook.com>"
PDF_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZgABEgAQAPdf2b4p0X1EkQh0rVzy4w0="
LOGO_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZgABEgAQAJ2g1Yg5mGJGo9Uq3FxZ0Bk="
TXT_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZgABEgAQAKqk8Bf2o1tCnI1u5wUe7lo="
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
WAMIDS = (
    "wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI1RjQyNUE3NEYxMzAzMzQ5MkEA",
    "wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI2QkJDMzQ5RTA0MUE2NzQwQzIA",
)
MEDIA_ID = "1013859600285441"


def e2e_message(clock: FakeClock) -> dict[str, Any]:
    raw = graph_message(
        E2E_ID,
        subject="WA | (650) 555-1234 | BK12345",
        body={"contentType": "html", "content": outlook(*LINES)},
        author="Elizabeth@vship2000.com",  # Exchange keeps the letter case the user has
        name="Elizabeth Reyes",
        sender=None,
        auth=("Internal",),
        headers=(),
        message_class="IPM.Note",
        received=iso(clock() - 200),
        internet_id=E2E_INTERNET_ID,
    )
    raw["@odata.context"] = f"{GRAPH}/$metadata#users('{MAILBOX}')/messages(...)/$entity"
    return raw


E2E_ATTACHMENTS = {
    "@odata.context": f"{GRAPH}/$metadata#users('{MAILBOX}')/messages('...')/attachments",
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "@odata.mediaContentType": "application/pdf",
            "id": PDF_ID,
            "name": "Booking BK12345.pdf",
            "contentType": "application/pdf",
            "size": 48213,
            "isInline": False,
            "contentId": "f_lq3x9z0a0",  # Outlook gives attached files a content id too
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "@odata.mediaContentType": "image/png",
            "id": LOGO_ID,
            "name": "image001.png",
            "contentType": "image/png",
            "size": 6512,
            "isInline": True,
            "contentId": LOGO_CID,
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "@odata.mediaContentType": "text/plain",
            "id": TXT_ID,
            "name": "notes.txt",
            "contentType": "text/plain",
            "size": 120,
            "isInline": False,
            "contentId": None,
        },
    ],
}


def meta_template(name: str, body: str, header: str | None = None) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if header:
        components.append(
            {"type": "HEADER", "format": header, "example": {"header_handle": ["4::YXBwbGljYXRpb24="]}}
        )
    components.append({"type": "BODY", "text": body, "example": {"body_text": [["Maria", "It arrived."]]}})
    return {
        "name": name,
        "parameter_format": "POSITIONAL",
        "components": components,
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "id": "1387372356726668",
    }


def meta_templates(request: httpx.Request) -> httpx.Response:
    entries = {
        "vship_update": meta_template("vship_update", UPDATE_BODY),
        "vship_document": meta_template("vship_document", DOCUMENT_BODY, header="DOCUMENT"),
    }
    name = request.url.params["name"]
    return httpx.Response(200, json={"data": [entries[name]] if name in entries else []})


def meta_sent(wamid: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": PHONE, "wa_id": "16505551234"}],
            "messages": [{"id": wamid, "message_status": "accepted"}],
        },
    )


def e2e_routes(clock: FakeClock) -> dict[str, respx.Route]:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"token_type": "Bearer", "expires_in": 3599, "access_token": "graph-T"}
        )
    )
    return {
        "message": respx.get(E2E_URL).mock(return_value=httpx.Response(200, json=e2e_message(clock))),
        "attachments": respx.get(f"{E2E_URL}/attachments").mock(
            return_value=httpx.Response(200, json=E2E_ATTACHMENTS)
        ),
        "download": respx.get(f"{E2E_URL}/attachments/{quote(PDF_ID, safe='')}/$value").mock(
            return_value=httpx.Response(200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"})
        ),
        "folders": respx.get(f"{BOX}/mailFolders").mock(return_value=httpx.Response(200, json={"value": []})),
        "create": respx.post(f"{BOX}/mailFolders").mock(
            return_value=httpx.Response(201, json={"id": "AAMkFOLDER0001=", "displayName": FOLDER})
        ),
        "move": respx.post(f"{E2E_URL}/move").mock(return_value=httpx.Response(201, json={"id": E2E_ID})),
        "templates": respx.get(f"{META}/{WABA}/message_templates").mock(side_effect=meta_templates),
        "upload": respx.post(f"{META}/{NUMBER_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": MEDIA_ID})
        ),
        "send": respx.post(f"{META}/{NUMBER_ID}/messages").mock(side_effect=[meta_sent(w) for w in WAMIDS]),
    }


async def run_e2e(settings, store: Store, clock: FakeClock) -> int:
    key = f"relay:{E2E_ID}"
    assert store.enqueue(KIND_RELAY, key, {"id": E2E_ID, "received": iso(clock() - 200)}, clock())
    item = store.get(key)
    assert item is not None
    async with httpx.AsyncClient() as http:
        oauth = OAuthTokenProvider(http, settings)
        processor = RelayProcessor(
            settings,
            store,
            GraphMailbox(http, settings, oauth, settings.relay_mailbox_address),
            WhatsAppClient(http, settings),
            TemplateGuard(http, settings, clock),
            FakeDirectory(matched()),
            clock=clock,
        )
        return await processor.process(item)


def e2e_notice(store: Store) -> EmailMessage:
    [item] = [i for i in store.list_by_status(STATUS_PENDING) if i.kind == KIND_NOTE]
    assert item.key == f"note:relay:{E2E_ID}:result"
    return from_payload(item.payload)


def check_graph_side(routes: dict[str, respx.Route]) -> None:
    request = routes["message"].calls.last.request
    assert request.headers["Prefer"] == 'IdType="ImmutableId", outlook.body-content-type="html"'
    assert request.headers["Authorization"] == "Bearer graph-T"
    assert "internetMessageHeaders" in request.url.params["$select"]
    assert routes["download"].call_count == 1  # the PDF only: never the logo or the text file
    assert routes["attachments"].calls.last.request.headers["Prefer"] == 'IdType="ImmutableId"'
    assert json.loads(routes["create"].calls.last.request.content) == {"displayName": FOLDER}
    assert json.loads(routes["move"].calls.last.request.content) == {"destinationId": "AAMkFOLDER0001="}
    upload = routes["upload"].calls.last.request
    assert upload.headers["Authorization"] == "Bearer wa-token"
    form = upload.content
    assert PDF_BYTES in form and b"application/pdf" in form and b'name="messaging_product"' in form
    assert b'filename="Booking BK12345.pdf"' in form


UPDATE_SENT = {
    "messaging_product": "whatsapp",
    "recipient_type": "individual",
    "to": PHONE,
    "type": "template",
    "template": {
        "name": "vship_update",
        "language": {"code": "en_US"},
        "components": [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": "Maria"}, {"type": "text", "text": MESSAGE}],
            }
        ],
    },
}


@respx.mock
async def test_end_to_end_inside_the_window_with_graph_and_meta(tmp_path, clock):
    settings = make_settings(tmp_path, **RELAY_ON)
    store = Store(settings.store_path)
    payload = text_payload(wamid="wamid.CUSTOMER1", from_phone="16505551234")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(int(clock() - 3600))
    assert store.enqueue(KIND_MESSAGE, "wamid.CUSTOMER1", {"envelope": payload}, clock() - 3598)
    routes = e2e_routes(clock)
    assert await run_e2e(settings, store, clock) == 2
    check_graph_side(routes)
    assert routes["templates"].call_count == 1  # checked again before the send, from the kept answer
    assert routes["templates"].calls.last.request.url.params["name"] == "vship_update"
    update, document = (json.loads(call.request.content) for call in routes["send"].calls)
    assert update == UPDATE_SENT
    assert document == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": PHONE,
        "type": "document",
        "document": {"id": MEDIA_ID, "filename": "Booking BK12345.pdf"},
    }
    sent = store.get_relay_sent(WAMIDS[0])
    assert sent is not None and sent["what"] == "update"
    sent = store.get_relay_sent(WAMIDS[1])
    assert sent is not None and sent["what"] == "file:Booking BK12345.pdf"
    notice = e2e_notice(store)
    assert notice["Subject"] == f"WhatsApp update sent: {NUMBER} (booking BK12345)"
    assert notice["To"] == f"Elizabeth Reyes <{EMPLOYEE}>"
    assert notice["In-Reply-To"] == E2E_INTERNET_ID
    text = text_of(notice)
    assert "  Hello Maria, VShip has an update:\n  " + MESSAGE in text
    assert "VShipCRM: Customer #42: Acme Imports / John Doe" in text
    assert "Sent: Booking BK12345.pdf (as its own WhatsApp message)" in text
    assert f"NOT sent: notes.txt - {NOT_SUPPORTED}" in text
    assert "image001.png" not in text
    assert PASTED_PICTURES_NOTE in text
    assert not await asyncio.to_thread(lambda: list(Path(settings.spool_dir).glob("relay_*")))


@respx.mock
async def test_end_to_end_outside_the_window_the_pdf_goes_with_the_document_template(tmp_path, clock):
    settings = make_settings(
        tmp_path,
        **RELAY_ON,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    store = Store(settings.store_path)
    routes = e2e_routes(clock)
    assert await run_e2e(settings, store, clock) == 2
    check_graph_side(routes)
    assert sorted(call.request.url.params["name"] for call in routes["templates"].calls) == [
        "vship_document",
        "vship_update",
    ]
    update, document = (json.loads(call.request.content) for call in routes["send"].calls)
    assert update == UPDATE_SENT
    assert document["type"] == "template"
    assert document["template"] == {
        "name": "vship_document",
        "language": {"code": "en_US"},
        "components": [
            {
                "type": "header",
                "parameters": [
                    {"type": "document", "document": {"id": MEDIA_ID, "filename": "Booking BK12345.pdf"}}
                ],
            },
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "Maria"},
                    {"type": "text", "text": "Booking BK12345.pdf"},
                ],
            },
        ],
    }
    text = text_of(e2e_notice(store))
    assert "Sent: Booking BK12345.pdf (with the WhatsApp document template)" in text
    assert f"NOT sent: notes.txt - {NOT_SUPPORTED}" in text


async def test_a_file_the_mail_program_marked_inline_is_still_an_attached_file(tmp_path, clock):
    """Owner decision (21 Nov 2026): Apple Mail / iPhone Mail can mark an attached PDF inline (and show it by
    content id); only a picture counts as pasted into the text, so the PDF goes like any attached file."""
    rig = Rig(tmp_path, clock)
    rig.customer_wrote()
    files = [
        (
            attachment("A1", "Invoice 123.pdf", "application/pdf", inline=True, content_id="<inv@apple>"),
            b"%PDF",
        ),
        (attachment("A2", "image001.png", "image/png", inline=True, content_id=LOGO_CID), b"logo"),
    ]
    html = outlook(*LINES, extra='<p class="MsoNormal"><img src="cid:inv@apple"><o:p></o:p></p>')
    item = rig.email(html=html, attachments=files)
    assert await rig.run(item) == 2
    assert [m["type"] for m in rig.whatsapp.sent] == ["template", "document"]
    assert rig.whatsapp.uploads == [("Invoice 123.pdf", "application/pdf", b"%PDF")]
    text = text_of(rig.notes()[0][1])
    assert "Sent: Invoice 123.pdf (as its own WhatsApp message)" in text
    assert "image001.png" not in text and PASTED_PICTURES_NOTE in text
