"""End to end: "WhatsApp updates from email" with every real part wired together as the service wires them.

The service starts through its real lifespan (``create_app`` in a ``TestClient``): a real Store on tmp_path,
the real Worker, and the RelayPoller, RelayProcessor, GraphMailbox, WhatsAppClient and TemplateGuard that
``app.main.build_relay`` builds. Only the world outside the service is replaced:

* Microsoft identity (the client-credentials token), Microsoft Graph (the relay mailbox: the Inbox listing,
  each email with its Outlook HTML, internet headers and attachments, file downloads, folders, moving) and
  Meta's Graph API (the template list, /messages, /media) answer through respx, from fakes that keep what
  the service sent them and the state of the mailbox;
* the mail transport is a fake that keeps every email the worker sends.

The worker and the poller do their startup work as in production (startup recovery, spool cleaning, the
Inbox cursor), but their loops are not run: each test takes the loops' steps itself (a poller round, then
the worker's ``claim_next`` and ``handle``), so nothing depends on timing and nothing sleeps. The tests at
the end are the defects this end-to-end run found, kept so that they stay fixed.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import HTTP
from email.utils import format_datetime, formataddr, getaddresses
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
import pytest
import respx
from app import main as main_module
from app.main import create_app
from app.relay.files import NOT_SUPPORTED, PASTED_PICTURES_NOTE
from app.relay.poller import CURSOR_KEY, SWITCHED_ON_KEY, RelayPoller, from_iso, to_iso
from app.relay.processor import INTERRUPTED
from app.security import compute_signature
from app.store import (
    KIND_MESSAGE,
    KIND_RELAY_STATUS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    QueueItem,
    Store,
)
from app.worker import RELAY_OFF_TEXT, Worker
from fastapi.testclient import TestClient

from tests.conftest import load_payload, make_settings, text_payload

MAILBOX = "whatsapp@example.com"  # the relay mailbox: GRAPH_SENDER and MAIL_FROM, as in production
EMPLOYEE = "dana.whitfield@example.com"
EMPLOYEE_NAME = "Dana Whitfield"
COLLEAGUE = "ops.lead@example.com"
STRANGER = "offers@freight-deals.example"
TENANT = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
BOX_PATH = f"/v1.0/users/{MAILBOX}"
NUMBER_ID = "106540352242922"  # the business number of the webhook fixtures
WABA = "102290129340398"
API = "/v27.0"
PHONE = "+16505551234"
SHOWN = "+1 650-555-1234"  # the number as the employee's emails show it
FOLDER = "WhatsApp updates - handled"
# the team's WhatsApp inbox (MAIL_TO of the test settings); ALERT_TO is not set, so alerts go there too
MAIL_TO = {"ops@example.com", "second@example.com"}
ALERT_TO = MAIL_TO
TEMPLATE_TEXT = (
    "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at"
    " bookings@example.com if you have any Questions"
)
TRACKING = "https://www.track-trace.example/container?no=MSCU1234567"
# Defender's Safe Links wraps the links of the emails it scans, internal ones included
SAFELINK = (
    "https://nam12.safelinks.protection.outlook.com/?url="
    + quote(TRACKING, safe="")
    + "&data=05%7C02%7Cdana.whitfield%40example.com%7C8c1b2f3e%7C0%7C0%7C638675%7CUnknown"
    + "&sdata=Yt3%2BqLZ8wXc1%3D&reserved=0"
)
COMMAND = (  # the email text as typed in Outlook (raw HTML per line: the link is Outlook's)
    "Name:Maria Lopez",
    "Message:",
    "Your container MSCU 1234567 has arrived at the port of Newark.",
    f'Track it here: <a href="{escape(SAFELINK)}">{TRACKING}</a>',
    "Customs release is expected on Friday.",
    "##",
)
MESSAGE = (
    "Your container MSCU 1234567 has arrived at the port of Newark. Track it here:"
    f" {TRACKING} Customs release is expected on Friday."
)
CUSTOMER_SAW = "\n".join(  # the approved template filled in, as the employee's email quotes it
    (
        "  Hello Maria Lopez, VShip has an update:",
        f"  {MESSAGE}",
        "  Please either reply to this message or send us an email at bookings@example.com if you have any"
        " Questions",
    )
)
LOGO_CID = "image001.png@01DB3A7C.4F5E6D70"
UNDELIVERABLE = {
    "code": 131026,
    "title": "Message undeliverable",
    "message": "Message undeliverable",
    "error_data": {"details": "Message Undeliverable."},
    "href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/",
}
RATE_LIMITED = {
    "error": {
        "message": "(#130429) Rate limit hit",
        "type": "OAuthException",
        "code": 130429,
        "error_data": {
            "messaging_product": "whatsapp",
            "details": "Cloud API message throughput has been reached.",
        },
        "fbtrace_id": "AazFh2k3m4n5p6q7r8s9t0u",
    }
}
DOCUMENT_TEXT = "Hello {{1}}, VShip has sent you the document {{2}}."
GRAPH_UNAVAILABLE = {
    "error": {
        "code": "ServiceUnavailable",
        "message": "The service is temporarily unavailable. Please retry later.",
        "innerError": {"date": "2026-11-19T15:30:12", "request-id": "3f1c2b3a-0000-4000-8000-0123456789ab"},
    }
}


class Crash(BaseException):
    """The service dying at that very moment (a power cut, a kill): nothing of it runs any more."""


@dataclass(frozen=True)
class File:
    """An attachment of an email in the mailbox."""

    name: str
    content_type: str
    data: bytes
    inline: bool = False
    content_id: str | None = None


LOGO = File(
    "image001.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"logo" * 200, inline=True, content_id=LOGO_CID
)
INVOICE = File("Invoice 1001.pdf", "application/pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"invoice " * 500)
NOTES = File("notes.txt", "text/plain", b"internal notes, not for the customer\n")


# ----------------------------------------------------------------------------- the world outside the service
class Identity:
    """Microsoft identity platform: hands out client-credentials tokens for Graph."""

    def __init__(self, settings, problems: list[str]) -> None:
        self.settings = settings
        self.problems = problems
        self.tokens: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        expected = {
            "grant_type": "client_credentials",
            "client_id": self.settings.oauth_client_id,
            "client_secret": self.settings.oauth_client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }
        if form != expected:
            self.problems.append(f"token request with {sorted(form)}")
            return httpx.Response(400, json={"error": "invalid_request"})
        token = f"eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.graph-{len(self.tokens) + 1}"
        self.tokens.append(token)
        return httpx.Response(
            200,
            json={"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599, "access_token": token},
        )


_ROUTES = (
    ("GET", re.compile(r"/mailFolders/inbox/messages"), "list"),
    ("GET", re.compile(r"/messages/([^/]+)"), "get"),
    ("GET", re.compile(r"/messages/([^/]+)/attachments"), "attachments"),
    ("GET", re.compile(r"/messages/([^/]+)/attachments/([^/]+)/\$value"), "download"),
    ("GET", re.compile(r"/messages/([^/]+)/attachments/([^/]+)"), "attachment"),
    ("GET", re.compile(r"/mailFolders"), "folders"),
    ("POST", re.compile(r"/mailFolders"), "create_folder"),
    ("POST", re.compile(r"/messages/([^/]+)/move"), "move"),
)
_INBOX = "AQMkADI5NGQ1YmZiLTNlNWEALTQyYjktOGYyZi01YTllNGRmMjlkM2UALgAAA8Lh9-InboxAAAA"


class Exchange:
    """The relay mailbox in Exchange Online, as Microsoft Graph serves it to the service.

    Anything the service is not expected to do (another mailbox, a call without the Graph token or without
    immutable ids, marking an email read, deleting it ...) is refused and recorded as a problem.
    """

    def __init__(self, identity: Identity, problems: list[str]) -> None:
        self.identity = identity
        self.problems = problems
        self.messages: dict[str, dict[str, Any]] = {}  # Graph id -> the email as Graph returns it
        self.classes: dict[str, str] = {}  # Graph id -> message class (PidTagMessageClass)
        self.location: dict[str, str] = {}  # Graph id -> folder id
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        self.content: dict[str, bytes] = {}  # attachment id -> the file
        self.folders: dict[str, str] = {_INBOX: "Inbox"}  # folder id -> display name
        self.calls: list[tuple[str, ...]] = []  # (operation, ids ...)
        self.answers: dict[str, list[httpx.Response]] = {}  # operation -> answers given instead, in order
        self.count = 0

    # ------------------------------------------------------------------ the test's side
    def deliver(
        self,
        *,
        subject: str,
        html: str,
        received: float,
        author: str = EMPLOYEE,
        author_name: str = EMPLOYEE_NAME,
        sender: str | None = None,
        auth_as: tuple[str, ...] = ("Internal",),
        extra_headers: tuple[tuple[str, str], ...] = (),
        message_class: str = "IPM.Note",
        files: tuple[File, ...] = (),
    ) -> str:
        """An email arrives in the Inbox; returns its (immutable) Graph id."""
        self.count += 1
        n = self.count
        graph_id = f"AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AGnUPtcQC-Eiwmc39SmMp_gAAB{n:04d}AAA="
        internet_id = f"<PH0PR11MB5641{n:04d}9A3F2C7D1E6B8A40@PH0PR11MB5641.namprd11.prod.outlook.com>"
        sender = author if sender is None else sender
        self.messages[graph_id] = {
            "@odata.etag": f'W/"CQAAABYAAADtR3pGq3pW6nYg6Y2t1oCzAAAB{n:04d}"',
            "id": graph_id,
            "receivedDateTime": iso(received),
            "hasAttachments": any(not f.inline for f in files),
            "internetMessageId": internet_id,
            "subject": subject,
            "body": {"contentType": "html", "content": html},
            "sender": {"emailAddress": {"name": author_name, "address": sender}},
            "from": {"emailAddress": {"name": author_name, "address": author}},
            "toRecipients": [{"emailAddress": {"name": "WhatsApp", "address": MAILBOX}}],
            "ccRecipients": [],
            "replyTo": [],
            "internetMessageHeaders": [
                {"name": name, "value": value}
                for name, value in exchange_headers(
                    subject=subject,
                    author=author,
                    author_name=author_name,
                    internet_id=internet_id,
                    received=received,
                    auth_as=auth_as,
                    has_files=any(not f.inline for f in files),
                )
            ]
            + [{"name": name, "value": value} for name, value in extra_headers],
        }
        self.classes[graph_id] = message_class
        self.location[graph_id] = _INBOX
        self.attachments[graph_id] = []
        for index, file in enumerate(files, start=1):
            attachment_id = (
                f"AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AGnUPtcQ{n:04d}ABEgAQA{index:03d}x9g3VQ0lKr6Y5o2cJ8Z0="
            )
            self.attachments[graph_id].append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "@odata.mediaContentType": file.content_type,
                    "id": attachment_id,
                    "lastModifiedDateTime": iso(received),
                    "name": file.name,
                    "contentType": file.content_type,
                    "size": len(file.data) + 220,  # Graph counts the attachment item, not only the file
                    "isInline": file.inline,
                    "contentId": file.content_id,
                }
            )
            self.content[attachment_id] = file.data
        return graph_id

    def folder_of(self, graph_id: str) -> str:
        return self.folders[self.location[graph_id]]

    def in_inbox(self, graph_id: str) -> bool:
        return self.location[graph_id] == _INBOX

    def put_back(self, graph_id: str) -> None:
        """Someone drags an email back into the Inbox; with immutable ids it keeps its id."""
        self.location[graph_id] = _INBOX

    def opened(self, graph_id: str) -> int:
        """How often the service read the email itself (not just its line in the Inbox listing)."""
        return sum(1 for call in self.calls if call[0] == "get" and call[1] == graph_id)

    def touched(self, graph_id: str) -> list[str]:
        """Every call about this one email: reading it, its attachments, moving it."""
        return [call[0] for call in self.calls if len(call) > 1 and call[1] == graph_id]

    def fail_next(self, operation: str, *answers: httpx.Response) -> None:
        self.answers.setdefault(operation, []).extend(answers)

    # ------------------------------------------------------------------ Graph's side
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(BOX_PATH + "/"):
            return self._wrong(request, "another mailbox than the relay mailbox")
        if request.headers.get("Authorization") not in {f"Bearer {token}" for token in self.identity.tokens}:
            self.problems.append(f"{request.method} {path} without a Graph token")
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if not request.headers.get("Prefer", "").startswith('IdType="ImmutableId"'):
            self.problems.append(f"{request.method} {path} without Prefer: IdType=ImmutableId")
        rest = path[len(BOX_PATH) :]
        for method, pattern, operation in _ROUTES:
            found = pattern.fullmatch(rest)
            if request.method == method and found:
                self.calls.append((operation, *found.groups()))
                canned = self.answers.get(operation)
                if canned:
                    return canned.pop(0)
                return getattr(self, "_" + operation)(request, *found.groups())
        return self._wrong(request, "a call the service is not expected to make")

    def _wrong(self, request: httpx.Request, why: str) -> httpx.Response:
        self.problems.append(f"Graph: {request.method} {request.url.path}: {why}")
        return httpx.Response(400, json={"error": {"code": "BadRequest", "message": why}})

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        found = re.fullmatch(
            r"receivedDateTime ge (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", params.get("$filter", "")
        )
        if found is None or params.get("$orderby") != "receivedDateTime desc":
            return self._wrong(request, f"an Inbox listing Graph refuses: {dict(params)}")
        listed = sorted(
            (
                message
                for graph_id, message in self.messages.items()
                if self.location[graph_id] == _INBOX and message["receivedDateTime"] >= found.group(1)
            ),
            key=lambda message: message["receivedDateTime"],
            reverse=True,
        )
        top = int(params.get("$top", "10"))
        if len(listed) > top:
            return self._wrong(request, "more than one page (not needed by these tests)")
        fields = params.get("$select", "").split(",")
        value = [{"@odata.etag": m["@odata.etag"], **{k: m[k] for k in fields if k in m}} for m in listed]
        return httpx.Response(
            200,
            json={
                "@odata.context": f"https://graph.microsoft.com/v1.0/$metadata#users('{MAILBOX}')/mailFolders"
                "('inbox')/messages(id,receivedDateTime,from,subject)",
                "value": value,
            },
        )

    def _get(self, request: httpx.Request, graph_id: str) -> httpx.Response:
        message = self.messages.get(graph_id)
        if message is None:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "ErrorItemNotFound",
                        "message": "The specified object was not found in the store.",
                    }
                },
            )
        params = request.url.params
        fields = params.get("$select", "").split(",")
        answer = {
            "@odata.context": f"https://graph.microsoft.com/v1.0/$metadata#users('{MAILBOX}')/messages/$entity",
            "@odata.etag": message["@odata.etag"],
            **{key: message[key] for key in fields if key in message},
        }
        if "String 0x001A" in params.get("$expand", ""):
            answer["singleValueExtendedProperties"] = [{"id": "String 0x1a", "value": self.classes[graph_id]}]
        return httpx.Response(200, json=answer)

    def _attachments(self, request: httpx.Request, graph_id: str) -> httpx.Response:
        select = request.url.params.get("$select", "").split(",")
        value = []
        for attachment in self.attachments.get(graph_id, []):
            entry = {key: value for key, value in attachment.items() if key != "contentId"}
            if "microsoft.graph.fileAttachment/contentId" in select:
                entry["contentId"] = attachment["contentId"]
            value.append(entry)
        return httpx.Response(200, json={"value": value})

    def _attachment(self, request: httpx.Request, graph_id: str, attachment_id: str) -> httpx.Response:
        for attachment in self.attachments.get(graph_id, []):
            if attachment["id"] == attachment_id:
                return httpx.Response(200, json=attachment)
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})

    def _download(self, request: httpx.Request, graph_id: str, attachment_id: str) -> httpx.Response:
        data = self.content.get(attachment_id)
        if data is None:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return httpx.Response(200, content=data, headers={"Content-Type": "application/octet-stream"})

    def _folders(self, request: httpx.Request) -> httpx.Response:
        found = re.fullmatch(r"displayName eq '(.*)'", request.url.params.get("$filter", ""))
        name = found.group(1).replace("''", "'") if found else None
        value = [
            {"id": folder_id, "displayName": shown, "childFolderCount": 0, "isHidden": False}
            for folder_id, shown in self.folders.items()
            if shown == name
        ]
        return httpx.Response(200, json={"value": value})

    def _create_folder(self, request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content).get("displayName")
        if name in self.folders.values():
            return httpx.Response(409, json={"error": {"code": "ErrorFolderExists"}})
        folder_id = (
            f"AQMkADI5NGQ1YmZiLTNlNWEALTQyYjktOGYyZi01YTllNGRmMjlkM2UALgAAA{len(self.folders):03d}AAA="
        )
        self.folders[folder_id] = name
        return httpx.Response(201, json={"id": folder_id, "displayName": name, "childFolderCount": 0})

    def _move(self, request: httpx.Request, graph_id: str) -> httpx.Response:
        destination = json.loads(request.content).get("destinationId")
        if destination not in self.folders:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "folder"}})
        if graph_id not in self.messages:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        self.location[graph_id] = destination
        return httpx.Response(201, json={**self.messages[graph_id], "parentFolderId": destination})


class Meta:
    """Meta's Graph API for the WhatsApp Business account: the template list, messages and media."""

    def __init__(self, settings, problems: list[str]) -> None:
        self.settings = settings
        self.problems = problems
        self.proof = hmac.new(
            settings.wa_app_secret.encode(), settings.wa_access_token.encode(), hashlib.sha256
        ).hexdigest()
        self.templates = [
            template("vship_update", "en_US", TEMPLATE_TEXT),
            # Meta's name filter also returns similar names and other languages
            template("vship_update", "es", "Hola {{1}}, VShip tiene una novedad:\n{{2}}"),
            template("vship_update_reminder", "en_US", "Reminder for {{1}}", category="MARKETING"),
            template("vship_document", "en_US", DOCUMENT_TEXT, document_header=True),
        ]
        self.template_checks = 0
        self.attempts: list[dict[str, Any]] = []  # every message the service asked WhatsApp to send
        self.sent: list[dict[str, Any]] = []  # the ones WhatsApp took, with the wamid it answered
        self.uploads: list[dict[str, Any]] = []
        # For the next sends: "timeout", "crash", an answer, or what reaches the service after WhatsApp
        # took the message but before its answer does (called with the message's wamid).
        self.next_sends: list[str | httpx.Response | Callable[[str], Awaitable[None]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.headers.get("Authorization") != f"Bearer {self.settings.wa_access_token}":
            self.problems.append(f"Meta: {request.method} {request.url.path} without the access token")
            return httpx.Response(401, json={"error": {"code": 190, "message": "Invalid OAuth access token"}})
        if request.url.params.get("appsecret_proof") != self.proof:
            self.problems.append(f"Meta: {request.method} {request.url.path} without appsecret_proof")
            return httpx.Response(400, json={"error": {"code": 100, "message": "Invalid appsecret_proof"}})
        path = request.url.path
        if request.method == "GET" and path == f"{API}/{WABA}/message_templates":
            return self._templates(request)
        if request.method == "POST" and path == f"{API}/{NUMBER_ID}/messages":
            return self._message(request)
        if request.method == "POST" and path == f"{API}/{NUMBER_ID}/media":
            return self._media(request)
        self.problems.append(f"Meta: {request.method} {path} is not expected")
        return httpx.Response(400, json={"error": {"code": 100, "message": "Unsupported request"}})

    def _templates(self, request: httpx.Request) -> httpx.Response:
        self.template_checks += 1
        params = request.url.params
        if params.get("fields") != "name,status,category,language,components":
            self.problems.append(f"Meta: template list asked for {params.get('fields')!r}")
        wanted = params.get("name", "")
        return httpx.Response(
            200,
            json={
                "data": [entry for entry in self.templates if wanted in entry["name"]],
                "paging": {"cursors": {"before": "QVFIUmxtd2VBNlFZAZD", "after": "QVFIUjZAxYkNfZAWJn"}},
            },
        )

    def _message(self, request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        body = json.loads(request.content)
        self.attempts.append(body)
        if self.next_sends:
            plan = self.next_sends.pop(0)
            if plan == "timeout":  # the request went out; no answer came back in time
                raise httpx.ReadTimeout("The read operation timed out", request=request)
            if plan == "crash":
                raise Crash
            if callable(plan):  # respx awaits the answer
                return self._take_after(body, plan)
            assert isinstance(plan, httpx.Response)
            return plan
        return self._take(body)

    async def _take_after(
        self, body: dict[str, Any], first: Callable[[str], Awaitable[None]]
    ) -> httpx.Response:
        """WhatsApp takes the message, but ``first`` reaches the service before WhatsApp's answer does."""
        answer = self._take(body)
        await first(self.sent[-1]["wamid"])
        return answer

    def _take(self, body: dict[str, Any]) -> httpx.Response:
        """WhatsApp takes the message: its answer with the new message id."""
        wamid = f"wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI{len(self.sent) + 1:02d}QzRBNjU5OUFFRTAzODEwMTQ0RgA="
        self.sent.append({**body, "wamid": wamid})
        digits = "".join(ch for ch in str(body.get("to")) if ch.isdigit())
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": body.get("to"), "wa_id": digits}],
                "messages": [{"id": wamid, "message_status": "accepted"}],
            },
        )

    def _media(self, request: httpx.Request) -> httpx.Response:
        raw = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
        form: dict[str, Any] = {}
        for part in BytesParser(policy=HTTP).parsebytes(raw).iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True)
            if part.get_filename() is None:
                form[name] = data.decode()
            else:
                form[name] = {"filename": part.get_filename(), "type": part.get_content_type(), "data": data}
        self.uploads.append(form)
        return httpx.Response(200, json={"id": f"{1020304050607080 + len(self.uploads)}"})


class Crm:
    """VShipCRM: one customer with the number the employee writes to."""

    def __init__(self) -> None:
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, text="Service Unavailable")
        if request.url.path == "/api/Auth/login":
            return httpx.Response(200, json={"token": crm_jwt()})
        if request.url.path == "/api/Customer":
            customer = {
                "id": 42,
                "companyName": "Acme Imports",
                "contactPerson": "John Doe",
                "mobile": "(650) 555-1234",
                "email1": "john@acme.com",
                "email2": "ops@acme.com",
                "active": True,
            }
            return httpx.Response(
                200, json={"isSuccess": True, "isFailure": False, "error": None, "value": [customer]}
            )
        return httpx.Response(404)


class Outbox:
    """The mail transport: keeps every email the worker sends."""

    name = "outbox"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        self.sent.append(message)
        return message["Message-ID"] or ""

    def notices(self, kind: str | None = None) -> list[EmailMessage]:
        """The feature's own emails, all or of one kind (sent, refused, uncertain, dry_run, alert ...)."""
        return [m for m in self.sent if m["X-WhatsApp-Relay"] and kind in (None, m["X-WhatsApp-Relay"])]

    def forwarded(self) -> list[EmailMessage]:
        """The forwarder's emails: customers' WhatsApp messages to MAIL_TO."""
        return [m for m in self.sent if m["X-WhatsApp-Relay"] is None]


class World:
    """Everything outside the service."""

    def __init__(self, settings) -> None:
        self.problems: list[str] = []
        self.identity = Identity(settings, self.problems)
        self.exchange = Exchange(self.identity, self.problems)
        self.meta = Meta(settings, self.problems)
        self.crm = Crm()
        self.outbox = Outbox()

    @contextmanager
    def online(self) -> Iterator[None]:
        with respx.mock(assert_all_called=False) as router:  # any other address is refused
            router.post(TOKEN_URL).mock(side_effect=self.identity.handle)
            router.route(host="graph.microsoft.com").mock(side_effect=self.exchange.handle)
            router.route(host="graph.facebook.com").mock(side_effect=self.meta.handle)
            router.route(host="crm.example.test").mock(side_effect=self.crm.handle)
            yield
        assert self.problems == []


# ----------------------------------------------------------------------------- the service
class Service:
    """The service, started through its lifespan; the tests take the worker's and the poller's steps."""

    def __init__(self, settings, world: World) -> None:
        self.settings = settings
        self.world = world
        self.app = create_app(settings)
        self.client = TestClient(self.app)

    def __enter__(self) -> Service:
        self.client.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.__exit__(*exc)

    @property
    def state(self) -> Any:
        return self.app.state

    @property
    def store(self) -> Store:
        return self.state.store

    @property
    def worker(self) -> Worker:
        return self.state.worker

    @property
    def poller(self) -> RelayPoller:
        return self.state.relay_poller

    @property
    def relay(self) -> Any:
        return self.state.relay

    # ------------------------------------------------------------------ the loops' steps
    def poll(self) -> int:
        """One round of the poller, as its loop takes it: the Inbox listing, then the per-round check."""
        return self.client.portal.call(self._poll)

    async def _poll(self) -> int:
        added = await self.poller.poll_once()
        if self.poller.on_round is not None:
            self.poller.on_round()
        return added

    def step(self) -> str | None:
        """One step of the worker: the next item that is due now, if any. Returns its queue key."""
        return self.client.portal.call(self._step)

    async def _step(self) -> str | None:
        item = self.store.claim_next(self.worker.clock())
        if item is None:
            return None
        await self.worker.handle(item)
        return item.key

    def drain(self, limit: int = 100) -> list[str]:
        """Worker steps until the queue is empty; an item waiting for a retry is taken when it is due
        (only the queue's clock is moved, never the service's). Returns the keys in the order handled."""
        return self.client.portal.call(self._drain, limit)

    async def _drain(self, limit: int) -> list[str]:
        handled: list[str] = []
        for _ in range(limit):
            item = self.store.claim_next(self.worker.clock())
            if item is None:
                due = self.store.next_due_at()
                if due is None:
                    return handled
                item = self.store.claim_next(due)
                assert item is not None
            handled.append(item.key)
            await self.worker.handle(item)
        raise AssertionError(f"the queue did not empty in {limit} steps; last: {handled[-5:]}")

    def crash_step(self) -> str:
        """A worker step during which the process dies (Crash); its database is left as the crash left it."""
        return self.client.portal.call(self._crash_step)

    async def _crash_step(self) -> str:
        item = self.store.claim_next(self.worker.clock())
        assert item is not None
        try:
            await self.worker.handle(item)
        except Crash:
            return item.key
        raise AssertionError("the step did not crash")

    # ------------------------------------------------------------------ the HTTP side
    def webhook(self, payload: dict[str, Any]) -> httpx.Response:
        """A webhook POST from Meta, signed with WA_APP_SECRET."""
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": compute_signature(self.settings.wa_app_secret, body),
        }
        return self.client.post("/webhook", content=body, headers=headers)

    def health(self) -> dict[str, Any]:
        answer = self.client.get("/healthz")
        assert answer.status_code == 200
        return answer.json()

    def items(self, kind: str) -> list[QueueItem]:
        found = []
        for status in (STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE, STATUS_FAILED):
            found.extend(item for item in self.store.list_by_status(status, limit=1000) if item.kind == kind)
        return sorted(found, key=lambda item: item.id)


def _stopped() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@pytest.fixture
def start(monkeypatch):
    """``start(settings, world)``: the service as a context manager, with its loops left to the test.

    Worker.run and RelayPoller.run still do their startup work for real (the worker's recovery of items a
    crash left behind and its spool cleaning; the poller's Inbox cursor on the first switch-on): each runs
    with a stop signal that is already set, so it ends right after its startup, and then waits for the real
    stop of the lifespan.
    """
    worker_run, poller_run = Worker.run, RelayPoller.run

    async def worker_startup_only(self, stop: asyncio.Event) -> None:
        await worker_run(self, _stopped())
        await stop.wait()

    async def poller_startup_only(self, stop: asyncio.Event) -> None:
        await poller_run(self, _stopped())
        await stop.wait()

    monkeypatch.setattr(Worker, "run", worker_startup_only)
    monkeypatch.setattr(RelayPoller, "run", poller_startup_only)

    def factory(settings, world: World) -> Service:
        monkeypatch.setattr(main_module, "build_transport", lambda settings, http: world.outbox)
        return Service(settings, world)

    return factory


# ----------------------------------------------------------------------------- settings and data
def relay_settings(tmp_path: Path, **overrides: Any):
    """Production-like settings with WhatsApp updates switched on (in dry run unless overridden)."""
    base: dict[str, Any] = {
        "mail_transport": "graph",
        "graph_sender": MAILBOX,
        "oauth_tenant_id": TENANT,
        "oauth_client_id": "2a3b4c5d-6e7f-4801-9a2b-3c4d5e6f7a8b",
        "oauth_client_secret": "client-secret-value",
        "wa_waba_id": WABA,
        # The updates go out from this number. WA_PHONE_NUMBER_ID stays empty as in production, so the
        # forwarder's own filter is not switched on by the feature (spec section 12).
        "relay_phone_number_id": NUMBER_ID,
        "relay_enabled": True,
        "relay_allowed_senders": f"{EMPLOYEE}; {COLLEAGUE}",
        "worker_enabled": True,
        "crm_enabled": False,
        "crm_refresh_seconds": 3600,
    }
    return make_settings(tmp_path, **{**base, **overrides})


def switched_on_earlier(settings, minutes: float = 30) -> float:
    """The feature was switched on a while ago (the service has restarted since): recent mail counts.

    Returns the switch-on moment (to the second, as the poller keeps it).
    """
    moment = to_iso(time.time() - minutes * 60)
    store = Store(settings.store_path)
    try:
        store.set_meta(SWITCHED_ON_KEY, moment)
        store.set_meta(CURSOR_KEY, moment)
    finally:
        store.close()
    stamp = from_iso(moment)
    assert stamp is not None
    return stamp


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def template(
    name: str, language: str, body: str, *, category: str = "UTILITY", document_header: bool = False
) -> dict[str, Any]:
    """A template as Meta's message_templates list returns it."""
    components: list[dict[str, Any]] = []
    if document_header:
        components.append(
            {
                "type": "HEADER",
                "format": "DOCUMENT",
                "example": {"header_handle": ["4::YXBwbGljYXRpb24vcGRm:ARb"]},
            }
        )
    components.append({"type": "BODY", "text": body, "example": {"body_text": [["Maria", "Your update"]]}})
    return {
        "name": name,
        "parameter_format": "POSITIONAL",
        "components": components,
        "language": language,
        "status": "APPROVED",
        "category": category,
        "sub_category": "CUSTOM",
        "id": str(int(hashlib.sha256(f"{name}:{language}".encode()).hexdigest()[:13], 16)),
    }


def crm_jwt() -> str:
    """A token as the CRM's login hands it out (only its expiry is read)."""

    def segment(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header, claims = segment({"alg": "HS256", "typ": "JWT"}), segment({"sub": "svc", "exp": 4_000_000_000})
    return f"{header}.{claims}.{'x' * 43}"


_WORD_HEAD = (
    '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns:w="urn:schemas-microsoft-com:office:word"'
    ' xmlns:m="http://schemas.microsoft.com/office/2004/12/omml" xmlns="http://www.w3.org/TR/REC-html40">'
    '<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
    '<meta name="Generator" content="Microsoft Word 15 (filtered medium)">'
    "<style><!--\n@font-face\n\t{font-family:Aptos;}\np.MsoNormal, li.MsoNormal, div.MsoNormal"
    '\n\t{margin:0in;\n\tfont-size:12.0pt;\n\tfont-family:"Aptos",sans-serif;}'
    "\na:link, span.MsoHyperlink\n\t{color:#467886;\n\ttext-decoration:underline;}"
    "\nspan.EmailStyle17\n\t{mso-style-type:personal-compose;}\n--></style>"
    '<!--[if gte mso 9]><xml>\n<o:shapedefaults v:ext="edit" spidmax="1026" />\n</xml><![endif]-->'
    '</head><body lang="EN-US" link="#467886" vlink="#96607D" style="word-wrap:break-word">'
    '<div class="WordSection1">'
)
_SIGNATURE = (
    '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
    f'<p class="MsoNormal"><b><span style="font-size:10.0pt">{EMPLOYEE_NAME}</span></b><o:p></o:p></p>'
    '<p class="MsoNormal"><span style="font-size:10.0pt">Operations | VShip | Mobile: 650-555-0100</span>'
    "<o:p></o:p></p>"
    '<p class="MsoNormal"><img width="120" height="40" style="width:1.25in;height:.4166in"'
    f' id="Picture_x0020_1" src="cid:{LOGO_CID}" alt="VShip"><o:p></o:p></p>'
)


def outlook(*paragraphs: str) -> str:
    """The HTML body of a new email from Outlook for Windows: a paragraph per typed line, then the signature
    with its logo (a picture pasted into the text, not a file). Each line is HTML already."""
    body = "".join(
        f'<p class="MsoNormal">{line}<o:p></o:p></p>'
        if line
        else '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
        for line in paragraphs
    )
    return _WORD_HEAD + body + _SIGNATURE + "</div></body></html>"


def exchange_headers(
    *,
    subject: str,
    author: str,
    author_name: str,
    internet_id: str,
    received: float,
    auth_as: tuple[str, ...],
    has_files: bool,
) -> list[tuple[str, str]]:
    """The transport headers Exchange Online keeps with an email (Graph's internetMessageHeaders)."""
    when = format_datetime(datetime.fromtimestamp(received, UTC))
    internal = auth_as == ("Internal",)
    hop = "PH0PR11MB5641.namprd11.prod.outlook.com"
    headers = [
        (
            "Received",
            f"from {hop} (2603:10b6:510:d6::14) by PH7PR11MB6522.namprd11.prod.outlook.com"
            " (2603:10b6:510:27e::9) with Microsoft SMTP Server (version=TLS1_2,"
            f" cipher=TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384) id 15.20.8158.21; {when}",
        ),
    ]
    if internal:
        headers.append(
            (
                "Received",
                f"from {hop} ([fe80::7d1c:2b3a:4c5d:6e7f]) by {hop} ([fe80::7d1c:2b3a:4c5d:6e7f%4])"
                f" with mapi id 15.20.8158.020; {when}",
            )
        )
    else:
        headers += [
            (
                "Received",
                "from mail-out.freight-deals.example (203.0.113.25) by"
                " BN8NAM12FT047.mail.protection.outlook.com (10.13.182.140) with Microsoft SMTP Server;"
                f" {when}",
            ),
            (
                "Authentication-Results",
                "spf=pass (sender IP is 203.0.113.25) smtp.mailfrom=freight-deals.example; dkim=pass"
                " (signature was verified) header.d=freight-deals.example;dmarc=pass action=none"
                " header.from=freight-deals.example;compauth=pass reason=100",
            ),
        ]
    headers += [
        (
            "Content-Type",
            'multipart/related; boundary="_004_PH0PR11MB56419A3F2C7D1E6B_"; type="multipart/alternative"',
        ),
        ("From", formataddr((author_name, author))),
        ("To", f"WhatsApp <{MAILBOX}>"),
        ("Subject", subject),
        ("Thread-Topic", subject),
        ("Thread-Index", "AdtaB4Kq1m3n8Xo9QJ2wW0u0b0xQyg=="),
        ("Date", when),
        ("Message-ID", internet_id),
        ("Accept-Language", "en-US"),
        ("Content-Language", "en-US"),
        ("X-MS-Has-Attach", "yes" if has_files else ""),
        ("X-MS-Exchange-Organization-SCL", "-1" if internal else "1"),
        ("X-MS-TNEF-Correlator", ""),
        ("MIME-Version", "1.0"),
        ("X-MS-Exchange-Organization-MessageDirectionality", "Originating" if internal else "Incoming"),
        (
            "X-MS-Exchange-Organization-AuthSource",
            hop if internal else "BN8NAM12FT047.eop-nam12.prod.protection",
        ),
        *(("X-MS-Exchange-Organization-AuthAs", value) for value in auth_as),
        ("X-MS-Exchange-Organization-AuthMechanism", "04" if internal else "0a"),
        ("X-MS-Exchange-Organization-Network-Message-Id", "8c1b2f3e-4d5a-4b6c-9d7e-0f1a2b3c4d5e"),
        ("X-MS-PublicTrafficType", "Email"),
        ("X-MS-TrafficTypeDiagnostic", "PH0PR11MB5641:EE_|PH7PR11MB6522:EE_"),
        ("X-MS-Exchange-Organization-ExpirationStartTime", iso(received)),
        ("X-MS-Exchange-Organization-ExpirationStartTimeReason", "OriginalSubmit"),
        ("X-MS-Exchange-Organization-ExpirationInterval", "1:00:00:00.0000000"),
        ("X-MS-Exchange-Organization-ExpirationIntervalReason", "OriginalSubmit"),
        ("X-MS-Office365-Filtering-Correlation-Id", "8c1b2f3e-4d5a-4b6c-9d7e-0f1a2b3c4d5e"),
        ("X-Microsoft-Antispam", "BCL:0;ARA:13230040|1800799024|366016|376014;"),
        (
            "X-Forefront-Antispam-Report",
            f"CIP:255.255.255.255;CTRY:;LANG:en;SCL:-1;SRV:;IPV:NLI;SFV:SKI;H:{hop};PTR:;CAT:NONE;SFS:;"
            + ("DIR:INT;" if internal else "DIR:INB;"),
        ),
        ("X-MS-Exchange-CrossTenant-OriginalArrivalTime", when),
        ("X-MS-Exchange-CrossTenant-FromEntityHeader", "Hosted" if internal else "Internet"),
        ("X-MS-Exchange-CrossTenant-Id", TENANT),
        ("X-MS-Exchange-CrossTenant-AuthAs", "Internal" if internal else "Anonymous"),
        ("X-MS-Exchange-CrossTenant-AuthSource", hop),
        ("X-MS-Exchange-Transport-CrossTenantHeadersStamped", "PH7PR11MB6522"),
        ("X-MS-Exchange-Transport-EndToEndLatency", "00:00:01.2915370"),
        ("X-MS-Exchange-Processed-By-BccFoldering", "15.20.8158.000"),
        (
            "X-Microsoft-Antispam-Mailbox-Delivery",
            "ucf:0;jmr:0;auth:0;dest:I;ENG:(910001)(944506478)(944626604)(920097)(930097)(140003);",
        ),
    ]
    return headers


def customer_message(text: str, *, minutes_ago: float, wamid: str) -> dict[str, Any]:
    """A customer's WhatsApp text message as the webhook delivers it, sent ``minutes_ago``."""
    payload = text_payload(text, wamid=wamid)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(
        int(time.time() - minutes_ago * 60)
    )
    return payload


def status_report(wamid: str, status: str, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A delivery report (``statuses``) as the webhook delivers it."""
    payload = copy.deepcopy(load_payload("statuses"))
    entry: dict[str, Any] = {
        "id": wamid,
        "status": status,
        "timestamp": str(int(time.time())),
        "recipient_id": "16505551234",
    }
    if errors is not None:
        entry["errors"] = errors
    else:
        entry["pricing"] = {
            "billable": True,
            "pricing_model": "PMP",
            "category": "utility",
            "type": "regular",
        }
    payload["entry"][0]["changes"][0]["value"]["statuses"] = [entry]
    return payload


def text_of(message: EmailMessage) -> str:
    part = message.get_body(("plain",))
    assert part is not None
    return part.get_content()


def html_heading(message: EmailMessage) -> str:
    part = message.get_body(("html",))
    assert part is not None
    found = re.search(r"<h2[^>]*>(.*?)</h2>", part.get_content(), flags=re.S)
    assert found is not None
    return found.group(1)


def addresses(message: EmailMessage, header: str = "To") -> set[str]:
    return {address for _, address in getaddresses(message.get_all(header, []))}


def step_states(service: Service, graph_id: str) -> dict[str, str]:
    state = service.store.get_relay_state(f"relay:{graph_id}")
    assert state is not None
    return {step_id: step["status"] for step_id, step in state["steps"].items()}


# ----------------------------------------------------------------------------- (1) dry run
def test_dry_run_checks_the_command_tells_the_employee_and_sends_nothing(tmp_path, start):
    settings = relay_settings(tmp_path)  # RELAY_DRY_RUN is on by default
    world = World(settings)
    with world.online(), start(settings, world) as service:
        switched_on = from_iso(service.store.get_meta(SWITCHED_ON_KEY))  # the very first switch-on: now
        assert switched_on is not None and abs(switched_on - time.time()) < 60
        # already in the Inbox before the feature was switched on: never handled (owner decision 14)
        old = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=switched_on - 3600
        )
        # not a command: left completely untouched (decision 6)
        newsletter = world.exchange.deliver(
            subject="Port schedule for week 47",
            html=outlook("Dear customer,", "please find the new schedule below."),
            author="news@carrier.example",
            author_name="Carrier News",
            auth_as=("Anonymous",),
            extra_headers=(("List-Unsubscribe", "<mailto:leave@carrier.example>"),),
            received=switched_on,
        )
        command = world.exchange.deliver(
            subject="WA | (650) 555-1234 | B-1001",
            html=outlook(*COMMAND),
            received=switched_on,
            files=(LOGO,),
        )

        assert service.poll() == 1
        assert service.drain() == [f"relay:{command}", f"note:relay:{command}:result"]

        # nothing reached WhatsApp; the template was only looked up
        assert world.meta.attempts == [] and world.meta.uploads == []
        assert world.meta.template_checks == 1
        # the command was filed in the folder (created on first use); everything else is untouched
        assert world.exchange.folder_of(command) == FOLDER
        assert [call[0] for call in world.exchange.calls].count("create_folder") == 1
        assert world.exchange.in_inbox(old) and world.exchange.touched(old) == []
        assert world.exchange.in_inbox(newsletter) and world.exchange.touched(newsletter) == []

        (notice,) = world.outbox.sent  # the employee hears back; nobody else gets anything
        assert notice["X-WhatsApp-Relay"] == "dry_run"
        assert notice["From"] == f"WhatsApp updates <{MAILBOX}>"
        assert addresses(notice) == {EMPLOYEE} and notice["To"] == f"{EMPLOYEE_NAME} <{EMPLOYEE}>"
        assert notice["Subject"] == f"DRY RUN - WhatsApp update not sent: {SHOWN}"
        internet_id = world.exchange.messages[command]["internetMessageId"]
        assert notice["In-Reply-To"] == internet_id and notice["References"] == internet_id
        assert notice["Auto-Submitted"] == "auto-generated" and notice["X-Auto-Response-Suppress"] == "All"
        text = text_of(notice)
        assert "DRY RUN: this is what would have been sent. Nothing was sent to WhatsApp" in text
        assert "----- What the customer would receive -----\n" + CUSTOMER_SAW in text
        assert re.search(r"^Booking\s+: B-1001$", text, flags=re.M)
        assert "The booking number B-1001 is only shown in this email" in text
        assert PASTED_PICTURES_NOTE in text  # the signature logo is part of the text, never a file
        assert "safelinks" not in text

        relay_item = service.store.get(f"relay:{command}")
        assert relay_item is not None and relay_item.status == STATUS_DONE and relay_item.emails_sent == 0
        assert step_states(service, command) == {"notice": "sent", "filed": "sent"}

        relay = service.health()["relay"]
        assert relay["enabled"] is True and relay["dry_run"] is True and relay["test_mode"] is False
        assert relay["last_poll_ok_at"] is not None and relay["last_error"] is None
        assert relay["poller_alive"] is True
        assert relay["processor"]["decisions"] == {"dry_run": 1} and relay["processor"]["messages_sent"] == 0


# ----------------------------------------------------------------------------- (2) live
def test_live_the_update_is_sent_once_and_the_employee_gets_a_threaded_confirmation(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False, crm_enabled=True)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        command = world.exchange.deliver(
            subject="WA | 650.555.1234 | B-1001", html=outlook(*COMMAND), received=time.time() - 60
        )
        assert service.poll() == 1
        service.drain()

        (sent,) = world.meta.sent
        wamid = sent.pop("wamid")
        assert sent == {
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
                        "parameters": [
                            {"type": "text", "text": "Maria Lopez"},
                            {"type": "text", "text": MESSAGE},
                        ],
                    }
                ],
            },
        }
        record = service.store.get_relay_sent(wamid)
        assert record is not None and record["item_key"] == f"relay:{command}" and record["what"] == "update"

        (notice,) = world.outbox.sent  # no copy to MAIL_TO (owner decision 4)
        assert notice["X-WhatsApp-Relay"] == "sent" and addresses(notice) == {EMPLOYEE}
        assert notice["Subject"] == f"WhatsApp update sent: {SHOWN} (booking B-1001)"
        internet_id = world.exchange.messages[command]["internetMessageId"]
        assert notice["In-Reply-To"] == internet_id and notice["References"] == internet_id
        text = text_of(notice)
        assert f"WhatsApp accepted your update for {SHOWN}." in text
        assert "----- What the customer received -----\n" + CUSTOMER_SAW in text
        assert "VShipCRM: Customer #42: Acme Imports / John Doe" in text
        assert "Name: vship_update" in text and "Language: en_US" in text

        assert world.exchange.folder_of(command) == FOLDER
        relay_item = service.store.get(f"relay:{command}")
        assert relay_item is not None and relay_item.status == STATUS_DONE and relay_item.emails_sent == 1
        assert step_states(service, command) == {"template": "sent", "notice": "sent", "filed": "sent"}

        # The same email is handled once only: dragged back into the Inbox (it keeps its immutable id), it
        # is listed again but never queued again.
        world.exchange.put_back(command)
        assert service.poll() == 0
        assert service.drain() == []
        assert len(world.meta.attempts) == 1 and len(world.outbox.sent) == 1
        assert service.health()["relay"]["processor"]["messages_sent"] == 1


def test_files_follow_the_update_while_the_customer_service_window_is_open(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        # the customer wrote on WhatsApp 20 minutes ago: forwarded by email as always, and the window is open
        inbound = "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzRUIwQjMxNzE1OUQ3QzE2RTk0RgA="
        answer = service.webhook(
            customer_message("Is my container there yet?", minutes_ago=20, wamid=inbound)
        )
        assert answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}
        command = world.exchange.deliver(
            subject="WA | 650-555-1234",
            html=outlook(*COMMAND),
            received=time.time() - 30,
            files=(INVOICE, LOGO, NOTES),
        )
        assert service.poll() == 1
        service.drain()

        (forwarded,) = world.outbox.forwarded()
        assert addresses(forwarded) == MAIL_TO
        assert forwarded["Subject"] == f"[WhatsApp] Sheena Nelson ({SHOWN}): Is my container there yet?"

        template_message, document = world.meta.sent
        assert template_message["type"] == "template"
        (upload,) = world.meta.uploads
        assert upload["messaging_product"] == "whatsapp" and upload["type"] == "application/pdf"
        assert upload["file"] == {
            "filename": "Invoice 1001.pdf",
            "type": "application/pdf",
            "data": INVOICE.data,
        }
        assert document == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": PHONE,
            "type": "document",
            "document": {"id": "1020304050607081", "filename": "Invoice 1001.pdf"},
            "wamid": document["wamid"],
        }
        record = service.store.get_relay_sent(document["wamid"])
        assert record is not None and record["what"] == "file:Invoice 1001.pdf"

        (notice,) = world.outbox.notices()
        assert notice["X-WhatsApp-Relay"] == "sent"
        text = text_of(notice)
        assert "Sent: Invoice 1001.pdf (as its own WhatsApp message)" in text
        assert f"NOT sent: notes.txt - {NOT_SUPPORTED}" in text
        assert PASTED_PICTURES_NOTE in text and "image001.png" not in text
        assert list(Path(settings.spool_dir).glob("relay_*")) == []  # the downloaded file is gone again
        relay_item = service.store.get(f"relay:{command}")
        assert relay_item is not None and relay_item.status == STATUS_DONE and relay_item.emails_sent == 2


# ----------------------------------------------------------------------------- (3) a failed delivery
def test_a_failed_delivery_report_tells_the_employee_exactly_once(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        command = world.exchange.deliver(
            subject="WA | 650-555-1234 | B-1001", html=outlook(*COMMAND), received=time.time() - 60
        )
        assert service.poll() == 1
        service.drain()
        (sent,) = world.meta.sent
        wamid = sent["wamid"]
        assert [m["X-WhatsApp-Relay"] for m in world.outbox.sent] == ["sent"]

        report = status_report(wamid, "failed", errors=[UNDELIVERABLE])
        for _ in range(2):  # Meta delivers webhooks at least once: the same report can come twice
            answer = service.webhook(report)
            # Meta's answer is the same as without the feature
            assert answer.status_code == 200
            assert answer.json() == {"status": "ok", "queued": 0, "duplicates": 0}
        # A report that is not a failure is only counted. A failure of a message the feature did not send
        # (or has not recorded yet: the report can overtake WhatsApp's answer to the send) is queued as
        # well, and leads to nothing.
        service.webhook(status_report(wamid, "delivered"))
        other = "wamid.HBgLMTY1MDU1NTEyMzQVAgARGBJBQkNERUYwMTIzNDU2Nzg5AA=="
        service.webhook(status_report(other, "failed", errors=[UNDELIVERABLE]))

        status_items = service.items(KIND_RELAY_STATUS)
        assert [item.key for item in status_items] == [f"relay_status:{wamid}", f"relay_status:{other}"]
        service.drain()

        (failure,) = world.outbox.notices("delivery_failed")
        assert addresses(failure) == {EMPLOYEE}
        assert failure["Subject"] == f"WhatsApp could not deliver your update to {SHOWN}"
        internet_id = world.exchange.messages[command]["internetMessageId"]
        assert failure["In-Reply-To"] == internet_id and failure["References"] == internet_id
        text = text_of(failure)
        assert "Reason: WhatsApp could not deliver the message to this customer." in text
        assert "WhatsApp error code: 131026" in text
        assert "Your WhatsApp update (template vship_update)" in text
        record = service.store.get_relay_sent(wamid)
        assert record is not None and record["failure_notified"] is True

        # Meta repeats the report later on: nothing more is queued or sent
        assert service.webhook(report).json() == {"status": "ok", "queued": 0, "duplicates": 0}
        assert service.drain() == []
        assert [m["X-WhatsApp-Relay"] for m in world.outbox.sent] == ["sent", "delivery_failed"]
        assert len(world.meta.attempts) == 1


# ----------------------------------------------------------------------------- (4) not commands, not allowed
def test_replies_and_senders_who_may_not_send_updates_send_nothing_and_alert_once(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        now = time.time()
        # an allowed employee answers a confirmation and forwards an old command: not commands (decision 5)
        reply = world.exchange.deliver(
            subject="RE: WA | 650-555-1234 | B-1001",
            html=outlook("Thanks, that was quick!", "", *COMMAND),
            received=now - 240,
        )
        forward = world.exchange.deliver(
            subject="FW: WA | 650-555-1234", html=outlook("FYI", "", *COMMAND), received=now - 180
        )
        # a sender who is not on the allow-list (decision 2)
        stranger = world.exchange.deliver(
            subject="WA | 650-555-1234",
            html=outlook(*COMMAND),
            author=STRANGER,
            author_name="Freight Deals",
            auth_as=("Anonymous",),
            received=now - 120,
        )
        # someone outside typed an allowed address into From: Exchange did not sign them in (AuthAs)
        spoofed = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), auth_as=("Anonymous",), received=now - 90
        )
        # a delegate sent it on behalf of an allowed employee (From and Sender differ)
        delegated = world.exchange.deliver(
            subject="WA | 650-555-1234",
            html=outlook(*COMMAND),
            sender="assistant@example.com",
            received=now - 60,
        )
        # a Power Automate flow of an allowed employee: automatic mail is never a command, nor reported
        flow = world.exchange.deliver(
            subject="WA | 650-555-1234",
            html=outlook(*COMMAND),
            extra_headers=(
                ("X-MS-Mail-Application", "Microsoft Power Automate; User-Agent: azure-logic-apps/1.0"),
                ("X-MS-Mail-Operation-Type", "Send"),
            ),
            received=now - 30,
        )
        assert service.poll() == 4  # only the command-shaped subjects are queued
        service.drain()

        assert world.meta.attempts == [] and world.meta.template_checks == 0
        # nothing was moved (decisions 6 and 17)
        for graph_id in (reply, forward, stranger, spoofed, delegated, flow):
            assert world.exchange.in_inbox(graph_id)
        assert world.exchange.touched(reply) == [] and world.exchange.touched(forward) == []
        for graph_id in (stranger, spoofed, delegated, flow):  # read once to decide, then left alone
            assert world.exchange.touched(graph_id) == ["get"]

        (digest,) = world.outbox.sent  # no answer to anyone, one alert to the administrators
        assert digest["X-WhatsApp-Relay"] == "alert" and addresses(digest) == ALERT_TO
        assert digest["Subject"] == "[WhatsApp] ALERT: Emails from senders who may not send WhatsApp updates"
        text = text_of(digest)
        listed = re.findall(rf"^- .+: ({re.escape(STRANGER)}|{re.escape(EMPLOYEE)}) - ", text, flags=re.M)
        assert len(listed) == 1  # the first at once; the others are kept for the next digest
        for private in ("Maria", "container", "650", "B-1001"):  # addresses only, never what the email said
            assert private not in text
        assert service.relay.status()["not_allowed_waiting"] == 2  # the flow is not listed: automatic mail

        # later rounds within 24 hours send no second digest
        assert service.poll() == 0 and service.drain() == []
        assert len(world.outbox.sent) == 1


# ----------------------------------------------------------------------------- (5) no clear answer from Meta
def test_a_send_that_times_out_is_reported_as_uncertain_and_never_sent_again(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online():
        with start(settings, world) as service:
            command = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
            )
            assert service.poll() == 1
            world.meta.next_sends.append("timeout")  # the request went out; the answer never came
            world.exchange.fail_next("move", httpx.Response(503, json=GRAPH_UNAVAILABLE))  # filing fails once
            assert service.step() == f"relay:{command}"
            assert service.step() == f"note:relay:{command}:result"  # the employee is told at once
            assert service.step() is None  # the filing waits for its retry
            waiting = service.store.get(f"relay:{command}")
            assert waiting is not None and waiting.status == STATUS_PENDING
            assert world.exchange.in_inbox(command)
        # The service restarts before the retry (a deploy, a reboot): a new store connection, worker,
        # processor and poller on the same database.
        with start(settings, world) as service:
            assert service.poll() == 0  # still in the Inbox and listed again, but never queued twice
            service.drain()
            assert world.exchange.folder_of(command) == FOLDER
            done = service.store.get(f"relay:{command}")
            assert done is not None and done.status == STATUS_DONE and done.emails_sent == 0
            assert step_states(service, command) == {
                "template": "uncertain",
                "notice": "sent",
                "filed": "sent",
            }

    assert len(world.meta.attempts) == 1 and world.meta.sent == []  # tried once, never again
    (notice,) = world.outbox.sent
    assert notice["X-WhatsApp-Relay"] == "uncertain" and addresses(notice) == {EMPLOYEE}
    assert notice["Subject"] == f"WhatsApp update MAY NOT have been sent: {SHOWN}"
    internet_id = world.exchange.messages[command]["internetMessageId"]
    assert notice["In-Reply-To"] == internet_id
    text = text_of(notice)
    assert "WhatsApp did not confirm that it took the message, so it may or may not have been sent." in text
    assert "The service never sends the same update twice on its own, so it was not tried again." in text
    assert f"Open the chat with {SHOWN} in the WhatsApp Business app." in text


def test_a_crash_before_whatsapp_answered_never_leads_to_a_second_send(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online():
        with start(settings, world) as service:
            command = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
            )
            assert service.poll() == 1
            world.meta.next_sends.append("crash")  # the process dies while the request is out
            assert service.crash_step() == f"relay:{command}"
            crashed = service.store.get(f"relay:{command}")
            assert crashed is not None and crashed.status == STATUS_PROCESSING
            assert step_states(service, command) == {"template": "sending"}
        with start(settings, world) as service:  # the worker's startup recovery takes the item back
            recovered = service.store.get(f"relay:{command}")
            assert recovered is not None and recovered.status == STATUS_PENDING and recovered.crashes == 1
            service.drain()
            assert world.exchange.folder_of(command) == FOLDER
            assert step_states(service, command) == {
                "template": "uncertain",
                "notice": "sent",
                "filed": "sent",
            }

    assert len(world.meta.attempts) == 1 and world.meta.sent == []
    (notice,) = world.outbox.sent
    assert notice["X-WhatsApp-Relay"] == "uncertain"
    assert notice["Subject"] == f"WhatsApp update MAY NOT have been sent: {SHOWN}"
    assert INTERRUPTED in text_of(notice)


# ----------------------------------------------------------------------------- more of the owner's rules
def test_whatsapp_asking_to_wait_delays_only_this_update_which_is_then_sent_once(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False, crm_enabled=True)
    world = World(settings)
    world.crm.down = True  # VShipCRM cannot be reached: the update goes all the same (decision 10)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        command = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
        )
        assert service.poll() == 1
        # WhatsApp plainly did not take it (a rate limit): tried again automatically (decision 13)
        world.meta.next_sends.append(httpx.Response(400, json=RATE_LIMITED))
        assert service.step() == f"relay:{command}"
        waiting = service.store.get(f"relay:{command}")
        assert waiting is not None and waiting.status == STATUS_PENDING
        assert "130429" in (waiting.last_error or "")
        assert service.worker.paused_until is None  # never the pause that would hold up the forwarding
        assert step_states(service, command) == {}  # nothing was sent, so nothing is marked
        # a customer's message arriving meanwhile is forwarded at once
        inbound = "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQ5QjFDMkQzRTRGNTA2MTcyODM5NEEA"
        answer = service.webhook(customer_message("Hello?", minutes_ago=1, wamid=inbound))
        assert answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}
        assert service.step() == inbound
        assert len(world.outbox.forwarded()) == 1 and world.outbox.notices() == []
        service.drain()  # the retry

        assert len(world.meta.attempts) == 2 and len(world.meta.sent) == 1
        (notice,) = world.outbox.notices()
        assert notice["X-WhatsApp-Relay"] == "sent"
        assert "VShipCRM: CRM lookup failed:" in text_of(notice)
        assert world.exchange.folder_of(command) == FOLDER


def test_outside_the_window_a_pdf_goes_with_the_document_template(tmp_path, start):
    settings = relay_settings(
        tmp_path,
        relay_dry_run=False,
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    world = World(settings)
    switched_on_earlier(settings)
    sheet = File(
        "rates.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK" * 300
    )
    with world.online(), start(settings, world) as service:
        # the customer has not written in the last 24 hours: only templates reach them
        command = world.exchange.deliver(
            subject="WA | 650-555-1234",
            html=outlook(*COMMAND),
            received=time.time() - 60,
            files=(INVOICE, sheet),
        )
        assert service.poll() == 1
        service.drain()

        update, document = world.meta.sent
        assert update["template"]["name"] == "vship_update"
        (upload,) = world.meta.uploads
        assert upload["file"]["filename"] == "Invoice 1001.pdf" and upload["file"]["data"] == INVOICE.data
        assert document["type"] == "template" and document["to"] == PHONE
        assert document["template"] == {
            "name": "vship_document",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {"id": "1020304050607081", "filename": "Invoice 1001.pdf"},
                        }
                    ],
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Maria Lopez"},
                        {"type": "text", "text": "Invoice 1001.pdf"},
                    ],
                },
            ],
        }
        assert world.meta.template_checks == 2  # the update template and the document template, once each
        (notice,) = world.outbox.notices()
        text = text_of(notice)
        assert "Sent: Invoice 1001.pdf (with the WhatsApp document template)" in text
        assert (
            "NOT sent: rates.xlsx - The customer has not written to VShip on WhatsApp in the last 24 hours."
            in text
        )
        assert "Save it as a PDF and send it again." in text
        assert world.exchange.folder_of(command) == FOLDER


def test_a_booking_number_selects_the_booking_template_once_it_is_set_up(tmp_path, start):
    settings = relay_settings(
        tmp_path,
        relay_dry_run=False,
        relay_booking_template_name="vship_booking_update",
        relay_booking_template_params="name,booking,message",
    )
    world = World(settings)
    world.meta.templates.append(
        template("vship_booking_update", "en_US", "Hello {{1}}, VShip has an update on booking {{2}}:\n{{3}}")
    )
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        now = time.time()
        with_booking = world.exchange.deliver(
            subject="WA | 650-555-1234 | B-1001", html=outlook(*COMMAND), received=now - 120
        )
        without = world.exchange.deliver(
            subject="WA | 650-555-1234 |", html=outlook(*COMMAND), received=now - 60
        )
        assert service.poll() == 2
        service.drain()

        by_template = {m["template"]["name"]: m["template"] for m in world.meta.sent}
        assert set(by_template) == {"vship_booking_update", "vship_update"}  # decision 3
        assert by_template["vship_booking_update"]["components"] == [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "Maria Lopez"},
                    {"type": "text", "text": "B-1001"},
                    {"type": "text", "text": MESSAGE},
                ],
            }
        ]
        notices = {n["In-Reply-To"]: n for n in world.outbox.notices("sent")}
        booked = notices[world.exchange.messages[with_booking]["internetMessageId"]]
        assert booked["Subject"] == f"WhatsApp update sent: {SHOWN} (booking B-1001)"
        assert f"  Hello Maria Lopez, VShip has an update on booking B-1001:\n  {MESSAGE}" in text_of(booked)
        plain = notices[world.exchange.messages[without]["internetMessageId"]]
        assert plain["Subject"] == f"WhatsApp update sent: {SHOWN}"  # an empty third field is no booking


def test_a_template_meta_moved_to_marketing_stops_every_update_with_one_alert(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    world.meta.templates[0]["category"] = "MARKETING"  # Meta re-categorised the approved template
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        now = time.time()
        first = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=now - 120
        )
        second = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=now - 60
        )
        assert service.poll() == 2
        service.drain()

        assert world.meta.attempts == []  # nothing is sent with it (decision 8)
        assert world.meta.template_checks == 1  # Meta's answer is kept for RELAY_TEMPLATE_CHECK_SECONDS
        refusals = world.outbox.notices("refused")
        assert len(refusals) == 2
        threads = {notice["In-Reply-To"] for notice in refusals}
        assert threads == {world.exchange.messages[g]["internetMessageId"] for g in (first, second)}
        for notice in refusals:
            assert addresses(notice) == {EMPLOYEE}
            assert notice["Subject"] == "WhatsApp update NOT sent: the WhatsApp template is not ready"
            assert "Its category at Meta is MARKETING, not UTILITY" in text_of(notice)
        (alert,) = world.outbox.notices("alert")  # at most one per template per day
        assert addresses(alert) == ALERT_TO
        assert alert["Subject"] == "[WhatsApp] ALERT: WhatsApp template vship_update cannot be used"
        assert "Its category at Meta is MARKETING, not UTILITY" in text_of(alert)
        assert world.exchange.folder_of(first) == FOLDER and world.exchange.folder_of(second) == FOLDER


def test_test_mode_sends_only_to_the_test_numbers(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False, relay_test_numbers="(650) 555-0100")
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        now = time.time()
        customer = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=now - 120
        )
        tester = world.exchange.deliver(
            subject="WA | +1 650 555 0100", html=outlook(*COMMAND), received=now - 60
        )
        assert service.poll() == 2
        service.drain()

        (sent,) = world.meta.sent
        assert sent["to"] == "+16505550100"
        (refused,) = world.outbox.notices("refused")
        assert refused["In-Reply-To"] == world.exchange.messages[customer]["internetMessageId"]
        assert refused["Subject"] == "WhatsApp update NOT sent: test mode is on (not a test number)"
        (confirmation,) = world.outbox.notices("sent")
        assert confirmation["In-Reply-To"] == world.exchange.messages[tester]["internetMessageId"]
        assert "Test mode is on: updates can only be sent to the test numbers" in text_of(confirmation)
        assert world.exchange.folder_of(customer) == FOLDER and world.exchange.folder_of(tester) == FOLDER
        assert service.health()["relay"]["test_mode"] is True


def test_a_command_found_after_a_long_outage_is_refused_as_too_old(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    now = time.time()
    # switched on two days ago; then the service stood still for 26 hours
    store = Store(settings.store_path)
    store.set_meta(SWITCHED_ON_KEY, to_iso(now - 2 * 86400))
    store.set_meta(CURSOR_KEY, to_iso(now - 26 * 3600))
    store.close()
    with world.online(), start(settings, world) as service:
        stale = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=now - 25 * 3600
        )
        fresh = world.exchange.deliver(subject="WA | 650-555-1234", html=outlook(*COMMAND), received=now - 60)
        assert service.poll() == 2
        service.drain()

        assert len(world.meta.sent) == 1  # only the recent one (decision 14)
        (confirmation,) = world.outbox.notices("sent")
        assert confirmation["In-Reply-To"] == world.exchange.messages[fresh]["internetMessageId"]
        (refused,) = world.outbox.notices("refused")
        assert refused["In-Reply-To"] == world.exchange.messages[stale]["internetMessageId"]
        assert refused["Subject"] == "WhatsApp update NOT sent: your email is too old"
        assert "more than 24 hours ago" in text_of(refused)
        assert world.exchange.folder_of(stale) == FOLDER and world.exchange.folder_of(fresh) == FOLDER


def test_a_given_up_update_is_reported_and_replaying_it_never_sends_twice(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        command = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
        )
        assert service.poll() == 1
        refused_move = httpx.Response(
            400,
            json={"error": {"code": "ErrorMoveCopyFailed", "message": "The move or copy operation failed."}},
        )
        world.exchange.fail_next("move", refused_move)  # Graph refuses to file it: given up at once
        service.drain()

        failed = service.store.get(f"relay:{command}")
        assert failed is not None and failed.status == STATUS_FAILED
        assert len(world.meta.sent) == 1
        assert [m["X-WhatsApp-Relay"] for m in world.outbox.sent] == ["sent", "alert"]
        alert = world.outbox.notices("alert")[0]
        assert alert["Subject"] == "[WhatsApp] ALERT: A WhatsApp update email could not be finished"
        text = text_of(alert)
        assert "The update: sent." in text and "The employee had already been told the outcome." in text

        # the administrator fixes the cause and runs: whatsapp-webhook replay-failed relay:<id>
        cli = Store(settings.store_path)
        try:
            assert cli.requeue(f"relay:{command}", time.time()) == 1
        finally:
            cli.close()
        service.drain()

        done = service.store.get(f"relay:{command}")
        assert done is not None and done.status == STATUS_DONE
        assert world.exchange.folder_of(command) == FOLDER
        assert len(world.meta.attempts) == 1  # the replay only finished the filing
        assert [m["X-WhatsApp-Relay"] for m in world.outbox.sent] == ["sent", "alert"]


def test_a_switch_to_dry_run_before_a_delayed_send_stops_it(tmp_path, start):
    live = relay_settings(tmp_path, relay_dry_run=False)
    world = World(live)
    switched_on_earlier(live)
    with world.online():
        with start(live, world) as service:
            command = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
            )
            assert service.poll() == 1
            world.meta.next_sends.append(httpx.Response(400, json=RATE_LIMITED))
            assert service.step() == f"relay:{command}"  # decided "send"; WhatsApp asked to wait
        # the administrator switches back to dry run and restarts the service before the retry
        with start(relay_settings(tmp_path), world) as service:
            service.drain()
            assert world.exchange.folder_of(command) == FOLDER

    assert len(world.meta.attempts) == 1 and world.meta.sent == []  # the decision is re-checked before a send
    (notice,) = world.outbox.sent
    assert notice["X-WhatsApp-Relay"] == "refused"
    assert notice["Subject"] == "WhatsApp update NOT sent: the service was switched to dry run"


def test_updates_queued_before_the_feature_was_switched_off_wait_and_go_out_once_it_is_on(tmp_path, start):
    on = relay_settings(tmp_path, relay_dry_run=False)
    off = relay_settings(tmp_path, relay_enabled=False)
    world = World(on)
    switched_on_earlier(on)
    with world.online():
        with start(on, world) as service:
            command = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
            )
            assert service.poll() == 1  # queued; the service is stopped before the worker gets to it
        with start(off, world) as service:
            assert service.step() == f"relay:{command}"  # looked at: nothing is decided or sent now
            waiting = service.store.get(f"relay:{command}")
            assert waiting is not None and waiting.status == STATUS_PENDING
            assert waiting.last_error == RELAY_OFF_TEXT
            assert service.webhook(text_payload()).json()["queued"] == 1
            assert service.step() == "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA="
            assert service.step() is None  # the customer's message went out; the update keeps waiting
            assert service.store.get_relay_state(f"relay:{command}") is None
        with start(on, world) as service:
            service.drain()
            assert world.exchange.folder_of(command) == FOLDER

    assert len(world.meta.sent) == 1
    assert [m["X-WhatsApp-Relay"] for m in world.outbox.sent] == [None, "sent"]


# ----------------------------------------------------------------------------- (6) the feature switched off
FORWARDED_TEXT = """WhatsApp message
Sender       : Sheena Nelson
Phone        : +1 650-555-1234
Received     : Sun 08 Jun 2025 16:59:43 EDT
Message type : text

----- Message -----
Does it come in another color?

----- VShipCRM -----
Match: Customer #42: Acme Imports / John Doe (exact match on mobile "(650) 555-1234")
Replies to this email go to: john@acme.com, ops@acme.com

This email was generated automatically by the WhatsApp email forwarder.
"""  # as the deployed forwarder (570fd03) writes it for the text fixture


def test_feature_off_the_webhook_and_the_forwarding_behave_as_before(tmp_path, start):
    on = relay_settings(tmp_path, relay_dry_run=False)
    off = relay_settings(tmp_path, relay_enabled=False, crm_enabled=True)
    world = World(on)
    switched_on_earlier(on)
    with world.online():
        with start(on, world) as service:  # the feature was on for a while and sent an update
            sent_before = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
            )
            assert service.poll() == 1
            service.drain()
            (update,) = world.meta.sent
        calls_before, notices_before = len(world.exchange.calls), len(world.outbox.sent)
        tokens_before, checks_before = len(world.identity.tokens), world.meta.template_checks

        with start(off, world) as service:  # ... and is switched off again
            assert service.state.relay is None and service.state.relay_poller is None
            assert service.state.relay_task is None and service.worker.relay is None
            # an employee emails a command meanwhile: nobody reads it, nothing is sent, nobody answers
            ignored = world.exchange.deliver(
                subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 30
            )
            handshake = service.client.get(
                "/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": off.wa_verify_token,
                    "hub.challenge": "1158201444",
                },
            )
            assert handshake.status_code == 200 and handshake.text == "1158201444"
            assert service.client.post("/webhook", json=text_payload()).status_code == 403  # unsigned
            assert service.webhook(text_payload()).json() == {"status": "ok", "queued": 1, "duplicates": 0}
            assert service.webhook(text_payload()).json() == {"status": "ok", "queued": 0, "duplicates": 1}
            # a failure report for the update sent while the feature was on is only counted, as before
            failed = service.webhook(status_report(update["wamid"], "failed", errors=[UNDELIVERABLE]))
            assert failed.json() == {"status": "ok", "queued": 0, "duplicates": 0}
            assert service.items(KIND_RELAY_STATUS) == []
            assert [item.kind for item in service.items(KIND_MESSAGE)] == [KIND_MESSAGE]
            service.drain()

            health = service.health()
            assert health["ok"] is True and health["relay"] == {"enabled": False}
            assert health["queue"]["failed"] == 0 and health["queue"]["pending"] == 0

    # the feature asked nothing of the mailbox, Microsoft identity or Meta while it was off
    assert len(world.exchange.calls) == calls_before and world.exchange.in_inbox(ignored)
    assert len(world.identity.tokens) == tokens_before and world.meta.template_checks == checks_before
    assert world.exchange.folder_of(sent_before) == FOLDER
    assert len(world.meta.attempts) == 1
    new = world.outbox.sent[notices_before:]
    (forwarded,) = new  # only the customer's message, to MAIL_TO
    assert [name for name, _ in forwarded.items()] == [
        "From",
        "To",
        "Reply-To",
        "Subject",
        "Date",
        "Message-ID",
        "X-WhatsApp-Message-Id",
        "X-WhatsApp-From",
        "X-WhatsApp-BSUID",
        "X-WhatsApp-Type",
        "X-WhatsApp-CRM-Match",
        "MIME-Version",
        "Content-Type",
    ]
    assert forwarded["From"] == f"Sheena Nelson via WhatsApp <{MAILBOX}>"
    assert forwarded["To"] == "ops@example.com, second@example.com"
    assert forwarded["Reply-To"] == "john@acme.com, ops@acme.com"
    assert (
        forwarded["Subject"] == "[WhatsApp] Sheena Nelson (+1 650-555-1234): Does it come in another color?"
    )
    assert (
        forwarded["X-WhatsApp-Message-Id"] == "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA="
    )
    assert forwarded["X-WhatsApp-From"] == PHONE and forwarded["X-WhatsApp-Type"] == "text"
    assert forwarded["X-WhatsApp-CRM-Match"] == "customer:42"
    assert text_of(forwarded) == FORWARDED_TEXT


# ----------------------------------------------------------------------------- defects found end to end
# app/mail/compose.py _render_html: the new title parameter was shadowed by the section loop (for title,
# body in sections), so every forwarded email's HTML heading showed its last section's name.
def test_feature_off_forwarded_emails_keep_their_html_heading(tmp_path, start):
    settings = relay_settings(tmp_path, relay_enabled=False)
    world = World(settings)
    with world.online(), start(settings, world) as service:
        assert service.webhook(text_payload()).json()["queued"] == 1
        service.drain()
    (forwarded,) = world.outbox.sent
    assert html_heading(forwarded) == "WhatsApp message"  # what the deployed forwarder (570fd03) shows


# The same shadowing gave the feature's emails the name of their last section as their HTML heading.
def test_the_employees_email_has_its_own_html_heading(tmp_path, start):
    settings = relay_settings(tmp_path)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        world.exchange.deliver(subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60)
        assert service.poll() == 1
        service.drain()
    (notice,) = world.outbox.sent
    assert html_heading(notice) == "WhatsApp update"


# app/relay/poller.py queued one round's emails in Graph's listing order (newest first), and the worker
# takes them in queue order: two updates written within one poll interval reached the customer in reverse.
def test_two_updates_written_within_one_poll_interval_reach_the_customer_in_order(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    first = "Your container has arrived at the port of Newark."
    second = "Correction: the container arrives on Friday, not today."
    with world.online(), start(settings, world) as service:
        now = time.time()
        for text, age in ((first, 25), (second, 5)):
            world.exchange.deliver(
                subject="WA | 650-555-1234",
                html=outlook("Name:Maria Lopez", "Message:", text, "##"),
                received=now - age,
            )
        assert service.poll() == 2  # both arrived since the last round
        service.drain()
    texts = [m["template"]["components"][0]["parameters"][1]["text"] for m in world.meta.sent]
    assert texts == [first, second]


# app/main.py queued a "failed" report only for a message id the relay had recorded, and the relay records
# it only once WhatsApp's answer to the send is in: a report that overtook that answer was lost for good.
def test_a_failed_report_that_overtakes_whatsapps_answer_still_reaches_the_employee(tmp_path, start):
    settings = relay_settings(tmp_path, relay_dry_run=False)
    world = World(settings)
    switched_on_earlier(settings)
    with world.online(), start(settings, world) as service:
        command = world.exchange.deliver(
            subject="WA | 650-555-1234", html=outlook(*COMMAND), received=time.time() - 60
        )
        assert service.poll() == 1
        answers: list[httpx.Response] = []
        recorded_first: list[object] = []

        async def failure_report_first(wamid: str) -> None:
            # WhatsApp took the update and found at once that it cannot deliver it; its webhook reaches the
            # service while the answer to the send is still on its way
            report = status_report(wamid, "failed", errors=[UNDELIVERABLE])
            answers.append(await asyncio.to_thread(service.webhook, report))
            recorded_first.append(service.store.get_relay_sent(wamid))

        world.meta.next_sends.append(failure_report_first)
        service.drain()

        (sent,) = world.meta.sent
        assert recorded_first == [None]  # the report really came before the message id was recorded
        (answer,) = answers
        assert answer.status_code == 200
        assert answer.json() == {"status": "ok", "queued": 0, "duplicates": 0}  # Meta's answer as before
        assert sorted(m["X-WhatsApp-Relay"] for m in world.outbox.sent) == ["delivery_failed", "sent"]
        (failure,) = world.outbox.notices("delivery_failed")
        assert addresses(failure) == {EMPLOYEE}
        assert failure["Subject"] == f"WhatsApp could not deliver your update to {SHOWN}"
        assert failure["In-Reply-To"] == world.exchange.messages[command]["internetMessageId"]
        assert "WhatsApp error code: 131026" in text_of(failure)
        record = service.store.get_relay_sent(sent["wamid"])
        assert record is not None and record["failure_notified"] is True
        assert len(world.meta.attempts) == 1
