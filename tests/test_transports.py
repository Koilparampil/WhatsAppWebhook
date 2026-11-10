from __future__ import annotations

import base64
import json
import socket
from email.message import EmailMessage
from email.policy import SMTP

import httpx
import pytest
import respx
from aiosmtpd.controller import Controller
from app.mail import transports
from app.mail.transports import GraphTransport, OAuthTokenProvider, SendError, SmtpTransport, build_transport

from tests.conftest import make_settings


class CollectingHandler:
    def __init__(self):
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 Message accepted"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_sink() -> tuple[CollectingHandler, Controller, int]:
    handler = CollectingHandler()
    port = free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    return handler, controller, port


@pytest.fixture
def smtp_sink():
    handler, controller, port = start_sink()
    try:
        yield handler, port
    finally:
        controller.stop()


def sample_message() -> EmailMessage:
    m = EmailMessage(policy=SMTP)
    m["From"] = "Sheena via WhatsApp <whatsapp@example.com>"
    m["To"] = "ops@example.com, second@example.com"
    m["Reply-To"] = "john@acme.com, ops@acme.com"
    m["Subject"] = "[WhatsApp] hello"
    m["Message-ID"] = "<wa.test@example.com>"
    m["X-WhatsApp-Message-Id"] = "wamid.test"
    m.set_content("plain body")
    m.add_alternative("<p>html body</p>", subtype="html")
    m.add_attachment(b"PDFDATA" * 10, maintype="application", subtype="pdf", filename="doc.pdf")
    return m


async def test_smtp_transport_delivers(tmp_path, smtp_sink):
    handler, port = smtp_sink
    settings = make_settings(tmp_path, smtp_port=port)
    transport = build_transport(settings, httpx.AsyncClient())
    assert isinstance(transport, SmtpTransport)
    result = await transport.send(sample_message())
    assert result == "<wa.test@example.com>"
    assert len(handler.envelopes) == 1
    env = handler.envelopes[0]
    assert env.mail_from == "whatsapp@example.com"
    assert set(env.rcpt_tos) == {"ops@example.com", "second@example.com"}
    assert b"Reply-To: john@acme.com, ops@acme.com" in env.content
    assert b'filename="doc.pdf"' in env.content


async def test_smtp_connection_refused_is_retriable(tmp_path):
    settings = make_settings(tmp_path, smtp_port=1)  # nothing listens there
    transport = SmtpTransport(settings)
    with pytest.raises(SendError) as exc:
        await transport.send(sample_message())
    assert exc.value.retriable is True


TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0/users/sender@example.com"


def graph_settings(tmp_path, **kw):
    return make_settings(
        tmp_path,
        mail_transport="graph",
        graph_sender="sender@example.com",
        oauth_tenant_id="tenant-id",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        **kw,
    )


@respx.mock
async def test_graph_mime_send(tmp_path):
    settings = graph_settings(tmp_path)
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    send_route = respx.post(f"{GRAPH}/sendMail").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        transport = build_transport(settings, http)
        assert isinstance(transport, GraphTransport)
        await transport.send(sample_message())
        await transport.send(sample_message())
    assert token_route.call_count == 1  # cached
    req = send_route.calls[0].request
    assert req.headers["Authorization"] == "Bearer T"
    assert req.headers["Content-Type"] == "text/plain"
    mime = base64.b64decode(req.content)
    assert b"Subject: [WhatsApp] hello" in mime and b"Reply-To: john@acme.com" in mime
    body = token_route.calls[0].request.content.decode()
    assert (
        "grant_type=client_credentials" in body
        and "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body
    )


@respx.mock
async def test_graph_draft_with_upload_session(tmp_path, monkeypatch):
    monkeypatch.setattr(transports, "GRAPH_MIME_SEND_LIMIT", 10)
    monkeypatch.setattr(transports, "GRAPH_INLINE_ATTACHMENT_LIMIT", 20)
    monkeypatch.setattr(transports, "GRAPH_UPLOAD_CHUNK", 32)
    settings = graph_settings(tmp_path)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    draft = respx.post(f"{GRAPH}/messages").mock(return_value=httpx.Response(201, json={"id": "DRAFT1"}))
    session = respx.post(f"{GRAPH}/messages/DRAFT1/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={"uploadUrl": "https://upload.example.test/u1"})
    )
    upload = respx.put("https://upload.example.test/u1").mock(return_value=httpx.Response(200, json={}))
    send = respx.post(f"{GRAPH}/messages/DRAFT1/send").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        transport = GraphTransport(http, settings, OAuthTokenProvider(http, settings))
        await transport.send(sample_message())
    payload = json.loads(draft.calls[0].request.content)
    assert payload["subject"] == "[WhatsApp] hello"
    assert payload["body"]["contentType"] == "html"
    assert [r["emailAddress"]["address"] for r in payload["replyTo"]] == ["john@acme.com", "ops@acme.com"]
    assert [r["emailAddress"]["address"] for r in payload["toRecipients"]] == [
        "ops@example.com",
        "second@example.com",
    ]
    assert {h["name"] for h in payload["internetMessageHeaders"]} == {"X-WhatsApp-Message-Id"}
    assert json.loads(session.calls[0].request.content)["AttachmentItem"]["size"] == 70
    ranges = [c.request.headers["Content-Range"] for c in upload.calls]
    assert ranges == ["bytes 0-31/70", "bytes 32-63/70", "bytes 64-69/70"]
    assert send.call_count == 1


@respx.mock
async def test_graph_errors_map_to_retriable_flags(tmp_path):
    settings = graph_settings(tmp_path)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    respx.post(f"{GRAPH}/sendMail").mock(
        return_value=httpx.Response(400, json={"error": {"message": "invalid MIME content"}})
    )
    async with httpx.AsyncClient() as http:
        transport = build_transport(settings, http)
        with pytest.raises(SendError) as exc:
            await transport.send(sample_message())
        assert exc.value.retriable is False
    respx.post(f"{GRAPH}/sendMail").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        transport = build_transport(settings, http)
        with pytest.raises(SendError) as exc:
            await transport.send(sample_message())
        assert exc.value.retriable is True
