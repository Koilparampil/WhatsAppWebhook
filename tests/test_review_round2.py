"""Regression tests for the second adversarial review round."""

from __future__ import annotations

import os
import shlex
import subprocess

import httpx
import pytest
import respx
from app.crm.phones import normalize_phones
from app.mail.compose import build_emails, chunk_size_for, plan_emails
from app.mail.transports import GraphTransport, OAuthTokenProvider
from app.store import KIND_MESSAGE, Store
from app.whatsapp.media import DownloadedMedia, safe_filename
from app.whatsapp.parser import parse_webhook
from app.worker import Worker

from tests.conftest import load_payload, make_settings, text_payload
from tests.test_compose import matched_lookup, no_match_lookup
from tests.test_transports import graph_settings, sample_message
from tests.test_worker import FakeDirectory, FakeTransport


def _media(tmp_path, size: int, name: str, mime: str = "application/pdf") -> DownloadedMedia:
    path = tmp_path / f"spooled-{size}"
    path.write_bytes(os.urandom(size))
    return DownloadedMedia(
        path=path, size=size, mime_type=mime, filename=name, sha256_hex="0" * 64, integrity_verified=True
    )


# ----------------------------------------------------------------------------- shell-safe rebuild commands
def test_rebuild_command_cannot_run_a_sender_controlled_command(tmp_path):
    hostile = "Invoice'$(touch INJECTED-MARKER)'.pdf"
    name = safe_filename(hostile, "x")
    assert "'" not in name and "$" not in name
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000)
    msg = parse_webhook(load_payload("image")).messages[0]
    media = _media(tmp_path, 1_200_000, name)
    emails = build_emails(msg, matched_lookup(), media, settings)
    body = emails[0].message.get_body(("plain",)).get_content()
    cat_line = next(line.strip() for line in body.splitlines() if line.strip().startswith("cat "))
    # executing the instruction exactly as emailed only concatenates files
    workdir = tmp_path / "rebuild"
    workdir.mkdir()
    for i, part in enumerate(emails, start=1):
        attachment = next(part.message.iter_attachments())
        (workdir / attachment.get_filename()).write_bytes(attachment.get_payload(decode=True))
        assert attachment.get_filename() == f"{name}.{i:03d}"
    subprocess.run(["bash", "-c", cat_line], cwd=workdir, check=True)  # noqa: S603, S607
    assert not (workdir / "INJECTED-MARKER").exists()
    assert (workdir / name).read_bytes() == media.path.read_bytes()


def test_rebuild_commands_quote_spaces_and_use_wildcards_for_many_parts(tmp_path):
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000)
    msg = parse_webhook(load_payload("image")).messages[0]
    few = build_emails(msg, matched_lookup(), _media(tmp_path, 1_200_000, "Q3 report (final).pdf"), settings)
    body = few[0].message.get_body(("plain",)).get_content()
    assert "cat " + shlex.quote("Q3 report (final).pdf.001") in body
    assert 'copy /b "Q3 report (final).pdf.001"+"Q3 report (final).pdf.002"' in body

    chunk = chunk_size_for(settings.mail_max_message_bytes)
    many = plan_emails(msg, matched_lookup(), _media(tmp_path, chunk * 20, "big.pdf"), settings)
    assert many.part_count == 20
    first = build_emails(msg, matched_lookup(), _media(tmp_path, chunk * 20, "big.pdf"), settings)[0]
    text = first.message.get_body(("plain",)).get_content()
    assert "cat big.pdf.[0-9][0-9][0-9] > big.pdf" in text
    assert 'copy /b "big.pdf.???" "big.pdf"' in text


# ----------------------------------------------------------------------------- spoofing
def test_bidi_and_zero_width_characters_are_removed(tmp_path, settings):
    assert safe_filename("invoice‮fdp.exe", "x") == "invoicefdp.exe"
    msg = parse_webhook(text_payload(name="Boss‮ via nimda​")).messages[0]
    m = build_emails(msg, matched_lookup(), None, settings)[0].message
    assert "‮" not in str(m["From"]) and "​" not in str(m["Subject"])


def test_display_name_cannot_impersonate_an_address(settings):
    msg = parse_webhook(text_payload(name="IT Helpdesk <helpdesk@vship2000.com>")).messages[0]
    m = build_emails(msg, no_match_lookup(), None, settings)[0].message
    assert m["From"].addresses[0].display_name == "IT Helpdesk helpdesk at vship2000.com via WhatsApp"
    assert m["From"].addresses[0].addr_spec == "whatsapp@example.com"


def test_sender_urls_are_not_clickable_but_map_links_are(settings):
    msg = parse_webhook(text_payload("log in at https://evil.example/login now")).messages[0]
    html = build_emails(msg, no_match_lookup(), None, settings)[0].message.get_body(("html",)).get_content()
    assert "<a href" not in html and "https://evil.example/login" in html
    loc = next(m for m in parse_webhook(load_payload("mixed")).messages if m.type == "location")
    html2 = build_emails(loc, no_match_lookup(), None, settings)[0].message.get_body(("html",)).get_content()
    assert '<a href="https://www.google.com/maps/search/?api=1&amp;query=19.4326,-99.1332">' in html2


# ----------------------------------------------------------------------------- deterministic split plan
def test_split_decision_does_not_depend_on_the_crm_result(tmp_path, settings):
    msg = parse_webhook(load_payload("image")).messages[0]
    chunk = chunk_size_for(settings.mail_max_message_bytes)
    # a file right at the single-part boundary used to flip between 1 and 2 parts with the body size
    for size in (chunk - 5_000, chunk, chunk + 1, chunk + 200_000):
        media = _media(tmp_path, size, "doc.pdf")
        a = plan_emails(msg, matched_lookup(), media, settings)
        b = plan_emails(msg, no_match_lookup(), media, settings)
        assert (a.part_count, a.chunk_size, a.signature) == (b.part_count, b.chunk_size, b.signature)


@respx.mock
async def test_resume_resends_everything_when_the_plan_changed(tmp_path, clock):
    data = os.urandom(2_500_000)
    respx.get("https://graph.facebook.com/v27.0/1385223789055226").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://lookaside.fbsbx.com/big",
                "mime_type": "image/jpeg",
                "file_size": len(data),
            },
        )
    )
    respx.get("https://lookaside.fbsbx.com/big").mock(return_value=httpx.Response(200, content=data))
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000)
    store = Store(settings.store_path)
    transport = FakeTransport()
    worker = Worker(
        settings, store, FakeDirectory(matched_lookup()), transport, httpx.AsyncClient(), clock=clock
    )
    store.enqueue(KIND_MESSAGE, "wamid.IMAGE123", {"envelope": load_payload("image")}, clock())
    item = store.claim_next(clock())
    store.set_progress(item.id, 2, "3x999/123")  # progress recorded under a different cut
    item = store.get_by_id(item.id)
    await worker.handle(item)
    names = [next(m.iter_attachments()).get_filename() for m in transport.sent]
    assert names[0].endswith(".001")  # started again from the first part
    rebuilt = b"".join(next(m.iter_attachments()).get_payload(decode=True) for m in transport.sent)
    assert rebuilt == data


# ----------------------------------------------------------------------------- parser attribution
def test_lone_contact_is_not_attributed_to_another_sender():
    payload = text_payload()
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append(
        {
            "from_user_id": "US.9999",
            "id": "wamid.OTHER",
            "timestamp": "1749416390",
            "type": "text",
            "text": {"body": "hi"},
        }
    )
    parsed = parse_webhook(payload)
    other = next(m for m in parsed.messages if m.wamid == "wamid.OTHER")
    assert other.from_phone is None and other.display_name is None
    assert other.from_bsuid == "US.9999"
    first = next(m for m in parsed.messages if m.wamid != "wamid.OTHER")
    assert first.display_name == "Sheena Nelson"


def test_contact_matching_ignores_plus_and_formatting():
    payload = text_payload(from_phone="16505551234")
    payload["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"] = "+1 650 555 1234"
    payload["entry"][0]["changes"][0]["value"]["contacts"].append(
        {"profile": {"name": "Other"}, "wa_id": "15550000000"}
    )
    msg = parse_webhook(payload).messages[0]
    assert msg.display_name == "Sheena Nelson"


# ----------------------------------------------------------------------------- MIME types
def test_message_and_multipart_media_types_are_attached_as_binary(tmp_path, settings):
    msg = parse_webhook(load_payload("image")).messages[0]
    for mime in ("message/rfc822", "multipart/mixed", "weird", "text/plain; charset=utf-8"):
        media = _media(tmp_path, 1000, "forwarded.eml", mime)
        att = next(build_emails(msg, matched_lookup(), media, settings)[0].message.iter_attachments())
        expected = "text/plain" if mime.startswith("text/plain") else "application/octet-stream"
        assert att.get_content_type() == expected
        assert att.get_payload(decode=True) == media.path.read_bytes()


# ----------------------------------------------------------------------------- CRM phone fields
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("650-555-1234 Fax 650-555-9999", ["+16505551234", "+16505559999"]),
        ("Cell 650-555-1234 Office 650-555-9999", ["+16505551234", "+16505559999"]),
        ("650-555-1234 or 650-555-9999", ["+16505551234", "+16505559999"]),
        ("650-555-1234 ext. 7", ["+16505551234"]),
        ("6505551234 x12", ["+16505551234"]),
        ("+1 (650) 555-1234 (cell)", ["+16505551234"]),
    ],
)
def test_phone_fields_with_labels(raw, expected):
    assert [k.e164 for k in normalize_phones(raw, "US")] == expected


# ----------------------------------------------------------------------------- Graph attachment thresholds
@respx.mock
async def test_graph_inline_attachment_falls_back_to_upload_session_on_413(tmp_path, monkeypatch):
    from app.mail import transports

    monkeypatch.setattr(transports, "GRAPH_MIME_SEND_LIMIT", 10)
    monkeypatch.setattr(transports, "GRAPH_UPLOAD_CHUNK", 64)
    settings = graph_settings(tmp_path)
    base = "https://graph.microsoft.com/v1.0/users/sender@example.com"
    respx.post("https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    respx.post(f"{base}/messages").mock(return_value=httpx.Response(201, json={"id": "D3"}))
    inline = respx.post(f"{base}/messages/D3/attachments").mock(return_value=httpx.Response(413))
    session = respx.post(f"{base}/messages/D3/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={"uploadUrl": "https://upload.example.test/u3"})
    )
    respx.put("https://upload.example.test/u3").mock(return_value=httpx.Response(201))
    sent = respx.post(f"{base}/messages/D3/send").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        await GraphTransport(http, settings, OAuthTokenProvider(http, settings)).send(sample_message())
    assert inline.call_count == 1 and session.call_count == 1 and sent.call_count == 1


@respx.mock
async def test_graph_failed_send_deletes_the_draft(tmp_path, monkeypatch):
    from app.mail import transports

    monkeypatch.setattr(transports, "GRAPH_MIME_SEND_LIMIT", 10)
    settings = graph_settings(tmp_path)
    base = "https://graph.microsoft.com/v1.0/users/sender@example.com"
    respx.post("https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    respx.post(f"{base}/messages").mock(return_value=httpx.Response(201, json={"id": "D4"}))
    respx.post(f"{base}/messages/D4/attachments").mock(return_value=httpx.Response(503))
    deleted = respx.delete(f"{base}/messages/D4").mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        with pytest.raises(transports.SendError):
            await GraphTransport(http, settings, OAuthTokenProvider(http, settings)).send(sample_message())
    assert deleted.call_count == 1
