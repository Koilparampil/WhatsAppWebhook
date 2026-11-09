from app.whatsapp.parser import parse_webhook

from tests.conftest import load_payload, media_payload, text_payload


def test_text_message_fields():
    parsed = parse_webhook(load_payload("text"))
    assert parsed.object_type == "whatsapp_business_account"
    assert len(parsed.messages) == 1
    msg = parsed.messages[0]
    assert msg.type == "text"
    assert msg.text == "Does it come in another color?"
    assert msg.from_phone == "16505551234"
    assert msg.phone_e164 == "+16505551234"
    assert msg.from_bsuid == "US.13491208655302741918"
    assert msg.display_name == "Sheena Nelson"
    assert msg.business_phone_number_id == "106540352242922"
    assert msg.timestamp == 1749416383
    assert msg.summary() == "Does it come in another color?"
    assert msg.sender_label == "Sheena Nelson"


def test_statuses_are_not_messages():
    parsed = parse_webhook(load_payload("statuses"))
    assert parsed.messages == []
    assert parsed.statuses == 1


def test_username_only_sender_has_no_phone():
    parsed = parse_webhook(text_payload(from_phone=None, username="sheena.n"))
    msg = parsed.messages[0]
    assert msg.from_phone is None
    assert msg.phone_e164 is None
    assert msg.username == "sheena.n"
    assert msg.display_name == "Sheena Nelson"
    parsed2 = parse_webhook(text_payload(from_phone=None, name=None, username="sheena.n"))
    assert parsed2.messages[0].sender_label == "@sheena.n"


def test_media_types_and_summaries():
    img = parse_webhook(load_payload("image")).messages[0]
    assert img.media is not None
    assert img.media.kind == "image"
    assert img.media.id == "1385223789055226"
    assert img.media.caption == "Here is the damaged box"
    assert img.summary() == "sent an image: Here is the damaged box"

    doc = parse_webhook(
        media_payload("document", filename="invoice.pdf", mime_type="application/pdf", caption=None)
    )
    m = doc.messages[0]
    assert m.media is not None and m.media.filename == "invoice.pdf"
    assert m.summary() == "sent a document: invoice.pdf"

    voice = parse_webhook(
        media_payload("audio", voice=True, mime_type="audio/ogg; codecs=opus", caption=None)
    )
    assert voice.messages[0].summary() == "sent a voice note"

    sticker = parse_webhook(media_payload("sticker", animated=True, mime_type="image/webp", caption=None))
    assert sticker.messages[0].media is not None and sticker.messages[0].media.animated is True


def test_mixed_batch_and_other_fields():
    parsed = parse_webhook(load_payload("mixed"))
    assert parsed.other_fields == ["message_template_status_update"]
    by_type = {m.type: m for m in parsed.messages}
    assert set(by_type) == {"location", "contacts", "interactive", "reaction", "unsupported"}

    loc = by_type["location"]
    assert loc.location is not None and loc.location.maps_url is not None
    assert "19.4326" in loc.location.maps_url
    assert loc.summary() == "shared a location: Zócalo"
    assert loc.display_name == "Ana"

    contacts = by_type["contacts"]
    assert contacts.from_phone is None  # Bob has a username; phone omitted
    assert contacts.username == "bob_the_builder"
    assert contacts.contacts[0].formatted_name == "Carlos Diaz"
    assert contacts.contacts[0].phones == ["+1 (650) 555-9999"]
    assert contacts.contacts[0].emails == ["carlos@example.com"]
    assert contacts.summary() == "shared 1 contact: Carlos Diaz"

    inter = by_type["interactive"]
    assert inter.interactive == {"type": "button_reply", "id": "yes-1", "title": "Yes, proceed"}
    assert inter.context is not None and inter.context.quoted_message_id == "wamid.OUTBOUND1"
    assert inter.summary() == "replied: Yes, proceed"

    assert by_type["reaction"].summary() == "reacted 👍"

    unsupported = by_type["unsupported"]
    assert unsupported.errors[0].code == 131051
    assert "not supported" in unsupported.errors[0].describe()


def test_phone_number_id_filter_and_garbage():
    parsed = parse_webhook(load_payload("text"), phone_number_id_filter="other")
    assert parsed.messages == [] and parsed.skipped_messages == 1
    assert parse_webhook(None).messages == []
    assert parse_webhook({"object": "page", "entry": "nope"}).messages == []
    assert (
        parse_webhook(
            {"entry": [{"changes": [{"field": "messages", "value": {"messages": [{"type": "text"}]}}]}]}
        ).messages
        == []
    )


def test_summary_truncation():
    parsed = parse_webhook(text_payload("x" * 200))
    assert len(parsed.messages[0].summary()) == 80
    assert parsed.messages[0].summary().endswith("…")
