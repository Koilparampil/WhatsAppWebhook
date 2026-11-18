from __future__ import annotations

import os
from email import message_from_bytes
from email.policy import SMTP

import pytest
from app.crm.directory import LookupResult, LookupStatus
from app.crm.phones import CrmRecord, MatchCandidate
from app.mail.compose import (
    ComposeError,
    build_alert_email,
    build_emails,
    chunk_size_for,
    encoded_attachment_size,
)
from app.whatsapp.media import DownloadedMedia
from app.whatsapp.parser import parse_webhook

from tests.conftest import load_payload, make_settings, text_payload


def matched_lookup(emails: list[str] | None = None) -> LookupResult:
    record = CrmRecord(
        entity="Customer",
        record_id="42",
        company="Acme Imports",
        contact="John Doe",
        active=True,
        updated_at=None,
        emails=emails or ["john@acme.com", "ops@acme.com"],
    )
    cand = MatchCandidate(record, "mobile", "650-555-1234", exact=True)
    return LookupResult(
        LookupStatus.MATCHED,
        candidates=[cand],
        reply_to=list(record.emails),
        index_age_seconds=120,
        entities_searched=["Customer"],
        lookup_key="+16505551234",
    )


def no_match_lookup() -> LookupResult:
    return LookupResult(
        LookupStatus.NO_MATCH,
        reason="no CRM record has phone +16505551234",
        index_age_seconds=10,
        entities_searched=["Customer"],
        lookup_key="+16505551234",
    )


def test_text_email_matched(settings):
    msg = parse_webhook(text_payload()).messages[0]
    emails = build_emails(msg, matched_lookup(), None, settings)
    assert len(emails) == 1
    m = emails[0].message
    assert m["From"] == "Sheena Nelson via WhatsApp <whatsapp@example.com>"
    assert m["To"] == "ops@example.com, second@example.com"
    assert m["Reply-To"] == "john@acme.com, ops@acme.com"
    assert m["Subject"] == "[WhatsApp] Sheena Nelson (+1 650-555-1234): Does it come in another color?"
    assert m["Message-ID"].startswith(
        "<wa.wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA@example.com>"
    )
    assert m["X-WhatsApp-CRM-Match"] == "customer:42"
    assert m["X-WhatsApp-From"] == "+16505551234"
    assert "In-Reply-To" not in m
    text = m.get_body(("plain",)).get_content()
    assert "Does it come in another color?" in text
    assert "Customer #42: Acme Imports / John Doe" in text
    assert "Replies to this email go to: john@acme.com, ops@acme.com" in text
    assert "Received                : " in text or "Received" in text
    html = m.get_body(("html",)).get_content()
    assert "Acme Imports" in html and "<script" not in html


def test_text_email_no_match_has_display_name_and_no_reply_to(settings):
    msg = parse_webhook(text_payload()).messages[0]
    m = build_emails(msg, no_match_lookup(), None, settings)[0].message
    assert "Reply-To" not in m
    assert m["X-WhatsApp-CRM-Match"] == "no_match"
    assert "Sheena Nelson" in m["Subject"]
    body = m.get_body(("plain",)).get_content()
    assert "No match: no CRM record has phone +16505551234" in body
    assert "Replies to this email will NOT reach the sender." in body


def test_fallback_reply_to_and_username_sender(tmp_path):
    settings = make_settings(tmp_path, mail_fallback_reply_to="frontdesk@example.com")
    msg = parse_webhook(text_payload(from_phone=None, name=None, username="sheena.n")).messages[0]
    lookup = LookupResult(LookupStatus.NO_PHONE, reason="no phone", entities_searched=["Customer"])
    m = build_emails(msg, lookup, None, settings)[0].message
    assert m["Reply-To"] == "frontdesk@example.com"
    assert m["From"] == '"@sheena.n via WhatsApp" <whatsapp@example.com>'
    assert m["X-WhatsApp-From"] == "unknown"
    assert "not shared by WhatsApp" in m.get_body(("plain",)).get_content()


def test_quoted_reply_threads(settings):
    msg = parse_webhook(text_payload(context={"from": "15550783881", "id": "wamid.ORIGINAL"})).messages[0]
    m = build_emails(msg, no_match_lookup(), None, settings)[0].message
    assert m["In-Reply-To"] == "<wa.wamid.ORIGINAL@example.com>"
    assert m["References"] == "<wa.wamid.ORIGINAL@example.com>"


def _media(tmp_path, size: int, name: str = "invoice.pdf", mime: str = "application/pdf") -> DownloadedMedia:
    path = tmp_path / name
    data = os.urandom(size)
    path.write_bytes(data)
    return DownloadedMedia(
        path=path, size=size, mime_type=mime, filename=name, sha256_hex="0" * 64, integrity_verified=True
    )


def test_single_attachment(settings, tmp_path):
    msg = parse_webhook(load_payload("image")).messages[0]
    media = _media(tmp_path, 20_000, "photo.jpg", "image/jpeg")
    emails = build_emails(msg, matched_lookup(), media, settings)
    assert len(emails) == 1
    m = emails[0].message
    atts = list(m.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "photo.jpg"
    assert atts[0].get_content_type() == "image/jpeg"
    assert atts[0].get_payload(decode=True) == media.path.read_bytes()
    assert "photo.jpg (19.5 KB, image/jpeg)" in m.get_body(("plain",)).get_content()
    assert "Here is the damaged box" in m["Subject"]


def test_split_large_attachment_into_parts(tmp_path):
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000)
    msg = parse_webhook(load_payload("image")).messages[0]
    media = _media(tmp_path, 2_500_000)
    emails = build_emails(msg, matched_lookup(), media, settings)
    assert len(emails) > 1
    original = media.path.read_bytes()
    rebuilt = b""
    for index, out in enumerate(emails, start=1):
        m = out.message
        assert out.part_index == index and out.part_count == len(emails)
        assert len(m.as_bytes()) <= settings.mail_max_message_bytes
        assert m["Subject"].endswith(f"[part {index}/{len(emails)}]")
        assert m["X-WhatsApp-Part"] == f"{index}/{len(emails)}"
        assert m["Reply-To"] == "john@acme.com, ops@acme.com"
        atts = list(m.iter_attachments())
        assert atts[0].get_filename() == f"invoice.pdf.{index:03d}"
        rebuilt += atts[0].get_payload(decode=True)
        body = m.get_body(("plain",)).get_content()
        assert f"part {index} of {len(emails)}" in body
        assert 'copy /b "invoice.pdf.001"+' in body and "cat invoice.pdf.001 invoice.pdf.002" in body
        if index > 1:
            assert m["In-Reply-To"] == emails[0].message["Message-ID"]
            assert m["Message-ID"] != emails[0].message["Message-ID"]
    assert rebuilt == original


def test_split_disabled_raises(tmp_path):
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000, mail_split_large_media=False)
    msg = parse_webhook(load_payload("image")).messages[0]
    with pytest.raises(ComposeError):
        build_emails(msg, matched_lookup(), _media(tmp_path, 2_000_000), settings)


def test_size_helpers():
    assert encoded_attachment_size(0) == 1024
    assert encoded_attachment_size(57) == 78 + 1024
    assert encoded_attachment_size(58) == 2 * 78 + 1024
    chunk = chunk_size_for(1_000_000, 10_000)
    assert chunk % 57 == 0
    assert 10_000 + encoded_attachment_size(chunk) < 1_000_000


def test_alert_email(settings):
    m = build_alert_email("something broke", "details here", settings)
    assert m["Subject"] == "[WhatsApp] ALERT: something broke"
    assert m["To"] == "ops@example.com, second@example.com"
    parsed = message_from_bytes(m.as_bytes(), policy=SMTP)
    assert parsed.get_content().strip() == "details here"


def test_chunk_size_is_deterministic_and_bounded():
    assert (
        chunk_size_for(32_000_000, 10_000)
        == chunk_size_for(32_000_000, 200_000)
        == chunk_size_for(32_000_000)
    )
    with pytest.raises(ComposeError):
        chunk_size_for(32_000_000, 300_000)


ERR_TYPE_NOT_SUPPORTED = {
    "code": 131051,
    "title": "Message type unknown",
    "message": "Message type unknown",
    "error_data": {"details": "Message type is currently not supported."},
}
ERR_FILE_TOO_BIG = {
    "code": 131052,
    "title": "Media file size too big. Max file size we currently support: 100MB.",
    "message": "Media file size too big. Max file size we currently support: 100MB.",
}
ERR_UNAVAILABLE = {
    "code": 131060,
    "title": "This message is currently unavailable.",
    "message": "This message is currently unavailable.",
}
NOT_AVAILABLE = "WhatsApp doesn't pass this kind of message to the forwarder, so its content isn't available."
RESEND = "Please ask the sender to send it again as a normal text, photo or file."


def unsupported_payload(label: str | None = "edit", errors: list[dict] | None = None) -> dict:
    payload = text_payload()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "unsupported"
    message["errors"] = [ERR_TYPE_NOT_SUPPORTED] if errors is None else errors
    if label is not None:
        message["unsupported"] = {"type": label}
    return payload


def render(payload: dict, settings, lookup: LookupResult | None = None):
    msg = parse_webhook(payload).messages[0]
    m = build_emails(msg, lookup or no_match_lookup(), None, settings)[0].message
    return m, m.get_body(("plain",)).get_content(), m.get_body(("html",)).get_content()


def test_unsupported_with_clear_label_is_described_in_plain_english(settings):
    m, text, html = render(unsupported_payload("edit"), settings)
    assert m["Subject"] == (
        "[WhatsApp] Sheena Nelson (+1 650-555-1234): edited an earlier message (content not available)"
    )
    lines = text.splitlines()
    assert "The sender edited a message they sent earlier." in lines
    assert NOT_AVAILABLE in lines
    assert RESEND in lines
    assert "WhatsApp error" not in text and "131051" not in text
    assert "The sender edited a message they sent earlier." in html and "131051" not in html


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "media_placeholder",
            "The sender sent a message WhatsApp can't pass on (WhatsApp's label: media_placeholder).",
        ),
        (None, "The sender sent a message WhatsApp can't pass on."),
    ],
)
def test_unsupported_unclear_or_missing_label_gets_generic_line(settings, label, expected):
    m, text, _ = render(unsupported_payload(label), settings)
    assert m["Subject"].endswith(": sent a message WhatsApp can't pass on")
    lines = text.splitlines()
    assert expected in lines
    assert NOT_AVAILABLE in lines
    assert RESEND in lines


def test_unsupported_file_too_big_gets_its_own_wording(settings):
    m, text, _ = render(unsupported_payload(None, errors=[ERR_FILE_TOO_BIG]), settings)
    assert m["Subject"].endswith(": sent a file WhatsApp couldn't download")
    lines = text.splitlines()
    assert "The sender attached a file WhatsApp couldn't download (for example, a file over 100 MB)." in lines
    assert "Please ask the sender to send the file another way, such as by email." in lines
    assert NOT_AVAILABLE not in lines
    assert RESEND not in lines
    assert "WhatsApp error" not in text


def test_unsupported_other_error_codes_keep_whatsapps_error_line(settings):
    _, text, _ = render(unsupported_payload("edit", errors=[ERR_UNAVAILABLE]), settings)
    assert "The sender edited a message they sent earlier." in text
    assert "WhatsApp error: code 131060 - This message is currently unavailable." in text


def test_removed_fields_stay_out_and_hidden_headers_stay(settings):
    payload = text_payload(
        context={
            "from": "15550783881",
            "id": "wamid.ORIGINAL",
            "forwarded": True,
            "referred_product": {"catalog_id": "CAT1", "product_retailer_id": "SKU-9"},
        }
    )
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["referral"] = {
        "source_url": "https://fb.me/vship-ad",
        "source_type": "ad",
        "headline": "Ship with VShip",
        "ctwa_clid": "CLID-1",
    }
    m, text, html = render(payload, settings, matched_lookup())
    for gone in (
        "WhatsApp user id",
        "US.13491208655302741918",
        "Sent to business number",
        "+15550783881",
        "Details",
        "WhatsApp message id",
        "Business phone number id",
        "In reply to message",
        "Referred product",
        "SKU-9",
        "CRM index age",
    ):
        assert gone not in text, gone
        assert gone not in html, gone
    message_section = text.split("----- Message -----")[1].split("----- VShipCRM -----")[0]
    assert "Forwarded message" in message_section
    assert "Came from an ad / link:" in message_section
    assert "  headline: Ship with VShip" in message_section
    assert "  click id: CLID-1" in message_section
    assert m["X-WhatsApp-Message-Id"] == "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA="
    assert m["X-WhatsApp-BSUID"] == "US.13491208655302741918"
    assert m["X-WhatsApp-From"] == "+16505551234"
    assert m["X-WhatsApp-Type"] == "text"
    assert m["X-WhatsApp-CRM-Match"] == "customer:42"
    assert m["In-Reply-To"] == "<wa.wamid.ORIGINAL@example.com>"


def test_unrecognised_type_is_named_without_raw_data(settings):
    payload = text_payload()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "brand_new_type"
    message["brand_new_type"] = {"secret": "raw-value"}
    _, text, _ = render(payload, settings)
    expected = "The sender sent a message the forwarder can't display (WhatsApp's label: brand_new_type)."
    assert expected in text.splitlines()
    assert "raw-value" not in text and "Raw message" not in text
