"""Delivery receipts (``value.statuses``) as :class:`StatusEvent` objects, which the "WhatsApp updates from
email" feature uses to tell an employee that an update it sent could not be delivered."""

from __future__ import annotations

import copy
from typing import Any

from app.whatsapp.parser import MessageError, StatusEvent, parse_webhook

from tests.conftest import load_payload, text_payload

OUR_NUMBER_ID = "106540352242922"  # metadata.phone_number_id of the fixtures

FAILED = {
    "id": "wamid.FAILED1",
    "status": "failed",
    "timestamp": "1749416600",
    "recipient_id": "16505551234",
    "errors": [
        {
            "code": 131049,
            "title": "This message was not delivered to maintain healthy ecosystem engagement.",
            "message": "This message was not delivered to maintain healthy ecosystem engagement.",
            "error_data": {
                "details": "In order to maintain a healthy ecosystem engagement, the message failed."
            },
            "href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/",
        }
    ],
}


def _statuses_payload(*statuses: Any, phone_number_id: str = OUR_NUMBER_ID) -> dict[str, Any]:
    payload = copy.deepcopy(load_payload("statuses"))
    value = payload["entry"][0]["changes"][0]["value"]
    value["statuses"] = list(statuses)
    value["metadata"]["phone_number_id"] = phone_number_id
    return payload


def test_delivered_receipt_from_the_fixture():
    parsed = parse_webhook(load_payload("statuses"))
    assert parsed.messages == []
    assert parsed.statuses == 1
    assert parsed.status_events == [
        StatusEvent(
            wamid="wamid.OUTBOUND1",
            status="delivered",
            recipient_id="16505551234",
            timestamp=1749416500,
            errors=[],
        )
    ]


def test_failed_status_carries_metas_error():
    parsed = parse_webhook(_statuses_payload(FAILED))
    (event,) = parsed.status_events
    assert (event.wamid, event.status, event.recipient_id, event.timestamp) == (
        "wamid.FAILED1",
        "failed",
        "16505551234",
        1749416600,
    )
    assert event.errors == [
        MessageError(
            code=131049,
            title="This message was not delivered to maintain healthy ecosystem engagement.",
            message="This message was not delivered to maintain healthy ecosystem engagement.",
            details="In order to maintain a healthy ecosystem engagement, the message failed.",
        )
    ]
    assert event.errors[0].describe().startswith("code 131049 - ")


def test_every_entry_of_every_change_in_order():
    sent = {"id": "wamid.A", "status": "sent", "timestamp": "1749416500", "recipient_id": "16505551234"}
    read = {"id": "wamid.A", "status": "read", "timestamp": "1749416510", "recipient_id": "16505551234"}
    payload = _statuses_payload(sent, FAILED)
    second = copy.deepcopy(payload["entry"][0])
    second["changes"][0]["value"]["statuses"] = [read]
    payload["entry"].append(second)
    # a change that carries both a customer message and a receipt
    mixed = text_payload()
    mixed["entry"][0]["changes"][0]["value"]["statuses"] = [dict(read, id="wamid.B")]
    payload["entry"].extend(mixed["entry"])

    parsed = parse_webhook(payload)
    assert [(e.wamid, e.status) for e in parsed.status_events] == [
        ("wamid.A", "sent"),
        ("wamid.FAILED1", "failed"),
        ("wamid.A", "read"),
        ("wamid.B", "read"),
    ]
    assert parsed.statuses == 4
    assert [m.text for m in parsed.messages] == ["Does it come in another color?"]


def test_phone_number_id_filter_is_the_same_as_for_messages():
    ours = parse_webhook(_statuses_payload(FAILED), phone_number_id_filter=OUR_NUMBER_ID)
    assert [e.wamid for e in ours.status_events] == ["wamid.FAILED1"] and ours.statuses == 1

    other = parse_webhook(_statuses_payload(FAILED), phone_number_id_filter="999999999")
    assert other.status_events == [] and other.statuses == 0
    assert other.skipped_phone_number_ids == {OUR_NUMBER_ID}
    assert other.skipped_messages == 0  # unchanged: only messages are counted as skipped

    # a batch for two business numbers: only ours is kept, receipts and messages alike
    payload = _statuses_payload(FAILED)
    theirs = _statuses_payload(dict(FAILED, id="wamid.THEIRS"), phone_number_id="999999999")
    theirs_message = text_payload(wamid="wamid.THEIR_MESSAGE")
    theirs_message["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999999999"
    payload["entry"].extend(theirs["entry"] + theirs_message["entry"])
    parsed = parse_webhook(payload, phone_number_id_filter=OUR_NUMBER_ID)
    assert [e.wamid for e in parsed.status_events] == ["wamid.FAILED1"]
    assert parsed.statuses == 1 and parsed.messages == [] and parsed.skipped_messages == 1
    unfiltered = parse_webhook(payload)
    assert [e.wamid for e in unfiltered.status_events] == ["wamid.FAILED1", "wamid.THEIRS"]


def test_entries_without_a_message_id_are_counted_but_give_no_event():
    entries = ["garbage", 42, None, {}, {"id": ""}, {"id": "   "}, {"status": "failed"}, {"id": "wamid.X"}]
    parsed = parse_webhook(_statuses_payload(*entries))
    assert parsed.statuses == len(entries)  # the count is unchanged
    assert parsed.status_events == [
        StatusEvent(wamid="wamid.X", status="", recipient_id=None, timestamp=0, errors=[])
    ]


def test_odd_values_are_normalised():
    parsed = parse_webhook(
        _statuses_payload(
            {"id": "wamid.U", "status": " FAILED ", "timestamp": "soon", "recipient_id": 16505551234},
            {"id": "wamid.V", "status": "failed", "errors": {"code": 131049}},
            {"id": "wamid.W", "status": "failed", "errors": ["oops", {"code": "131026"}]},
        )
    )
    first, second, third = parsed.status_events
    assert (first.wamid, first.status, first.timestamp, first.recipient_id) == (
        "wamid.U",
        "failed",
        0,
        "16505551234",
    )
    assert second.errors == []  # not a list
    assert third.errors == [
        MessageError(code=None, title=None, message="oops"),
        MessageError(code=131026, title=None, message=None, details=None),
    ]


def test_no_events_without_receipts_or_outside_the_messages_field():
    assert parse_webhook(text_payload()).status_events == []
    assert parse_webhook(load_payload("mixed")).status_events == []
    no_list = _statuses_payload()
    no_list["entry"][0]["changes"][0]["value"]["statuses"] = None
    parsed = parse_webhook(no_list)
    assert parsed.status_events == [] and parsed.statuses == 0
    other_field = _statuses_payload(FAILED)
    other_field["entry"][0]["changes"][0]["field"] = "message_template_status_update"
    parsed = parse_webhook(other_field)
    assert parsed.status_events == [] and parsed.statuses == 0
    assert parsed.other_fields == ["message_template_status_update"]
    assert parse_webhook(None).status_events == []


def test_status_event_can_be_built_by_position():
    event = StatusEvent("wamid.A", "failed", "16505551234", 1749416600)
    assert event.errors == []
    assert StatusEvent("wamid.A", "failed", None, 0, [MessageError(131049, "t")]).errors[0].code == 131049
