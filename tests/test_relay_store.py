"""Store additions for "WhatsApp updates from email": the relay step state, the record of sent WhatsApp
messages (failure reported once), the customer-service-window lookup, purging, and opening a database that
was created before the feature existed."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from app.store import (
    KIND_ALERT,
    KIND_MESSAGE,
    KIND_NOTE,
    KIND_RELAY,
    KIND_RELAY_STATUS,
    Store,
)

from tests.conftest import FakeClock, load_payload, text_payload

PHONE = "16505551234"  # the sender of tests/fixtures/payloads/text.json
TEXT_TIMESTAMP = 1749416383  # its message timestamp
DAY = 86400.0


def _sql(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run one statement on a second connection, the way an operator's sqlite3 shell would."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _inbound(
    store: Store,
    received_at: float,
    *,
    wamid: str,
    from_phone: str | None = PHONE,
    timestamp: Any = None,
    phone_number_id: str | None = None,
    message_type: str | None = None,
) -> None:
    """Queue an inbound WhatsApp message the way the webhook does: the envelope under payload['envelope']."""
    payload = text_payload(wamid=wamid, from_phone=from_phone)
    value = payload["entry"][0]["changes"][0]["value"]
    message = value["messages"][0]
    if timestamp is not None:
        message["timestamp"] = timestamp
    if phone_number_id is not None:
        value["metadata"]["phone_number_id"] = phone_number_id
    if message_type == "system":
        message["type"] = "system"
        message.pop("text")
        message["system"] = {
            "body": "User A changed from 16505551234 to 16505559999",
            "wa_id": "16505559999",
            "type": "user_changed_number",
        }
    assert store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, received_at)


# ----------------------------------------------------------------------------- kinds
def test_new_queue_kinds():
    assert (KIND_RELAY, KIND_RELAY_STATUS, KIND_NOTE) == ("relay", "relay_status", "note")
    assert len({KIND_MESSAGE, KIND_ALERT, KIND_RELAY, KIND_RELAY_STATUS, KIND_NOTE}) == 5


# ----------------------------------------------------------------------------- relay_state
def test_relay_state_round_trip_and_overwrite(tmp_path, clock: FakeClock):
    path = tmp_path / "q.db"
    store = Store(path)
    key = "relay:AAMkAGI2THVSAAA="
    assert store.get_relay_state(key) is None

    sending = {"decision": None, "steps": {"template": {"status": "sending"}}}
    store.save_relay_state(key, sending, clock())
    assert store.get_relay_state(key) == sending

    clock.advance(5)
    sent = {
        "decision": {"action": "send", "phone": "+16505551234", "name": "Zoë Ångström"},
        "steps": {"template": {"status": "sent", "wamid": "wamid.A"}, "file:att1": {"status": "sending"}},
    }
    store.save_relay_state(key, sent, clock())
    assert store.get_relay_state(key) == sent
    assert _sql(path, "SELECT key, updated_at FROM relay_state") == [(key, clock())]


def test_relay_state_returns_a_copy(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    store.save_relay_state("relay:1", {"decision": None, "steps": {}}, clock())
    first = store.get_relay_state("relay:1")
    assert first is not None
    first["steps"]["template"] = {"status": "sent"}
    assert store.get_relay_state("relay:1") == {"decision": None, "steps": {}}


def test_relay_state_is_per_key_and_survives_a_restart(tmp_path, clock: FakeClock):
    path = tmp_path / "q.db"
    store = Store(path)
    store.save_relay_state("relay:1", {"steps": {"template": {"status": "sent"}}}, clock())
    store.save_relay_state("relay:2", {"steps": {"template": {"status": "uncertain"}}}, clock())
    store.close()
    store = Store(path)
    assert store.get_relay_state("relay:1") == {"steps": {"template": {"status": "sent"}}}
    assert store.get_relay_state("relay:2") == {"steps": {"template": {"status": "uncertain"}}}
    assert store.get_relay_state("relay:3") is None


def test_save_relay_state_refuses_what_it_cannot_store_faithfully(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    with pytest.raises(TypeError):
        store.save_relay_state("relay:1", ["not", "a", "dict"], clock())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.save_relay_state("relay:1", {"when": object()}, clock())
    assert store.get_relay_state("relay:1") is None  # nothing half-written


@pytest.mark.parametrize("stored", ["[1, 2]", '"sent"', "not json"])
def test_unreadable_relay_state_raises_instead_of_starting_over(tmp_path, clock: FakeClock, stored):
    # None would tell the processor "nothing happened yet" and it could send the update a second time.
    path = tmp_path / "q.db"
    store = Store(path)
    _sql(path, "INSERT INTO relay_state (key, state, updated_at) VALUES (?, ?, ?)", ("relay:1", stored, 1.0))
    with pytest.raises(ValueError):
        store.get_relay_state("relay:1")


# ----------------------------------------------------------------------------- relay_sent
def test_record_and_read_a_sent_message(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    assert store.get_relay_sent("wamid.A") is None
    assert store.record_relay_sent("wamid.A", "relay:1", "update", clock()) is True
    assert store.get_relay_sent("wamid.A") == {
        "wamid": "wamid.A",
        "item_key": "relay:1",
        "what": "update",
        "created_at": clock(),
        "failure_notified": False,
    }
    created = clock()
    clock.advance(60)
    # the same wamid again keeps the first record
    assert store.record_relay_sent("wamid.A", "relay:2", "file:invoice.pdf", clock()) is False
    assert store.get_relay_sent("wamid.A") == {
        "wamid": "wamid.A",
        "item_key": "relay:1",
        "what": "update",
        "created_at": created,
        "failure_notified": False,
    }
    assert store.record_relay_sent("wamid.B", "relay:1", "file:invoice.pdf", clock()) is True
    assert store.get_relay_sent("wamid.B")["what"] == "file:invoice.pdf"


def test_record_relay_sent_needs_a_wamid(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    with pytest.raises(ValueError):
        store.record_relay_sent("", "relay:1", "update", clock())


def test_failure_is_notified_only_once(tmp_path, clock: FakeClock):
    path = tmp_path / "q.db"
    store = Store(path)
    store.record_relay_sent("wamid.A", "relay:1", "update", clock())
    store.record_relay_sent("wamid.B", "relay:1", "file:invoice.pdf", clock())

    assert store.mark_relay_failure_notified("wamid.A") is True
    assert store.mark_relay_failure_notified("wamid.A") is False  # Meta repeating the status: no 2nd email
    assert store.get_relay_sent("wamid.A")["failure_notified"] is True
    assert store.get_relay_sent("wamid.B")["failure_notified"] is False  # each message on its own
    assert store.mark_relay_failure_notified("wamid.not-ours") is False

    # recording the same wamid again does not reset the flag
    assert store.record_relay_sent("wamid.A", "relay:1", "update", clock()) is False
    assert store.mark_relay_failure_notified("wamid.A") is False

    store.close()
    store = Store(path)
    assert store.mark_relay_failure_notified("wamid.A") is False
    assert store.mark_relay_failure_notified("wamid.B") is True
    assert store.mark_relay_failure_notified("wamid.B") is False


# ----------------------------------------------------------------------------- recent_inbound_timestamp
def test_window_lookup_reads_the_real_webhook_envelope(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    payload = load_payload("text")
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, clock())
    since = clock() - DAY

    assert store.recent_inbound_timestamp(PHONE, since) == TEXT_TIMESTAMP
    # the E.164 form and any punctuation are fine: only the digits are compared
    assert store.recent_inbound_timestamp("+16505551234", since) == TEXT_TIMESTAMP
    assert store.recent_inbound_timestamp("+1 (650) 555-1234", since) == TEXT_TIMESTAMP
    # but the country code is part of the number
    assert store.recent_inbound_timestamp("6505551234", since) is None
    assert store.recent_inbound_timestamp("16505559999", since) is None
    assert store.recent_inbound_timestamp("", since) is None
    assert store.recent_inbound_timestamp("+", since) is None


def test_window_lookup_counts_media_messages(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    store.enqueue(KIND_MESSAGE, "wamid.IMAGE123", {"envelope": load_payload("image")}, clock())
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1749416400


def test_window_lookup_returns_the_newest_message_time(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    _inbound(store, clock(), wamid="wamid.1", timestamp="1700000100")
    clock.advance(1)
    _inbound(store, clock(), wamid="wamid.2", timestamp="1700000500")
    clock.advance(1)
    _inbound(store, clock(), wamid="wamid.3", timestamp="1700000300")
    _inbound(store, clock(), wamid="wamid.other", from_phone="16505559999", timestamp="1700000900")
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1700000500
    assert store.recent_inbound_timestamp("16505559999", clock() - DAY) == 1700000900


def test_window_lookup_uses_the_message_time_not_the_queue_time(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    since = clock() - DAY
    # queued before `since`: not looked at
    _inbound(store, since - 1, wamid="wamid.before", timestamp=str(int(clock())))
    assert store.recent_inbound_timestamp(PHONE, since) is None
    # exactly at `since`: looked at
    _inbound(store, since, wamid="wamid.edge", timestamp=str(int(clock()) - 3600))
    assert store.recent_inbound_timestamp(PHONE, since) == int(clock()) - 3600
    # Meta delivering a 3-day-old message only now: its own (old) time is returned, so the window stays shut
    three_days_ago = int(clock() - 3 * DAY)
    store2 = Store(tmp_path / "q2.db")
    _inbound(store2, clock(), wamid="wamid.late", timestamp=str(three_days_ago))
    assert store2.recent_inbound_timestamp(PHONE, since) == three_days_ago


def test_window_lookup_counts_every_queue_status_but_only_message_rows(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    _inbound(store, clock(), wamid="wamid.done", timestamp="1700000100")
    _inbound(store, clock(), wamid="wamid.failed", timestamp="1700000200")
    done = store.claim_next(clock())
    store.mark_done(done.id, emails_sent=1, now=clock())
    failed = store.claim_next(clock())
    store.mark_failed(failed.id, "gave up", clock())
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1700000200

    # rows of other kinds never count, even when they carry a matching envelope
    for kind in (KIND_ALERT, KIND_RELAY, KIND_RELAY_STATUS, KIND_NOTE):
        key = f"wamid.{kind}"
        store.enqueue(kind, key, {"envelope": text_payload(wamid=key)}, clock())
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1700000200


def test_window_lookup_ignores_messages_without_a_usable_sender_or_time(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    since = clock() - DAY
    _inbound(store, clock(), wamid="wamid.nophone", from_phone=None)  # username / BSUID only
    _inbound(store, clock(), wamid="wamid.t0", timestamp="0")
    _inbound(store, clock(), wamid="wamid.tneg", timestamp="-5")
    _inbound(store, clock(), wamid="wamid.tbad", timestamp="soon")
    assert store.recent_inbound_timestamp(PHONE, since) is None
    assert store.recent_inbound_timestamp("13491208655302741918", since) is None  # the BSUID's digits


def test_window_lookup_ignores_whatsapps_own_system_notices(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    _inbound(store, clock(), wamid="wamid.sys", timestamp="1700000100", message_type="system")
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) is None
    _inbound(store, clock(), wamid="wamid.text", timestamp="1700000050")
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1700000050


def test_window_lookup_can_be_limited_to_one_business_number(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    ours = "106540352242922"  # the fixture's business number
    _inbound(store, clock(), wamid="wamid.ours", timestamp="1700000100")
    _inbound(store, clock(), wamid="wamid.theirs", timestamp="1700000900", phone_number_id="999999999")
    since = clock() - DAY
    assert store.recent_inbound_timestamp(PHONE, since) == 1700000900
    assert store.recent_inbound_timestamp(PHONE, since, phone_number_id=ours) == 1700000100
    assert store.recent_inbound_timestamp(PHONE, since, phone_number_id="123") is None
    assert store.recent_inbound_timestamp(PHONE, since, phone_number_id="") == 1700000900  # "" = no filter


def test_window_lookup_only_trusts_the_message_the_row_is_for(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    store.enqueue(KIND_MESSAGE, "wamid.ROW", {"envelope": text_payload(wamid="wamid.OTHER")}, clock())
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) is None


@pytest.mark.parametrize(
    "payload", ["not json", "[1, 2]", "{}", '{"envelope": "garbage"}', '{"envelope": {"entry": 5}}']
)
def test_window_lookup_skips_unreadable_rows(tmp_path, clock: FakeClock, payload):
    path = tmp_path / "q.db"
    store = Store(path)
    _sql(
        path,
        "INSERT INTO queue (kind, key, payload, status, received_at, next_attempt_at)"
        " VALUES (?, ?, ?, 'pending', ?, ?)",
        (KIND_MESSAGE, "wamid.broken", payload, clock(), clock()),
    )
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) is None
    _inbound(store, clock(), wamid="wamid.good", timestamp="1700000100")
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == 1700000100


# ----------------------------------------------------------------------------- purge
def _finish(store: Store, kind: str, key: str, now: float, *, failed: bool = False) -> None:
    store.enqueue(kind, key, {}, now)
    item = store.claim_next(now)
    assert item is not None and item.key == key
    if failed:
        store.mark_failed(item.id, "gave up", now)
    else:
        store.mark_done(item.id, now=now)


def test_purge_also_clears_relay_state_and_old_sent_records(tmp_path, clock: FakeClock):
    path = tmp_path / "q.db"
    store = Store(path)
    state = {"decision": {"action": "send"}, "steps": {"template": {"status": "sent"}}}
    t0 = clock()
    _finish(store, KIND_RELAY, "relay:done-old", t0)
    _finish(store, KIND_RELAY, "relay:failed-old", t0, failed=True)
    for key in ("relay:done-old", "relay:failed-old", "relay:orphan"):
        store.save_relay_state(key, state, t0)
    store.record_relay_sent("wamid.old", "relay:done-old", "update", t0)
    store.record_relay_sent("wamid.failed-old", "relay:failed-old", "update", t0)

    clock.advance(40 * DAY)
    t1 = clock()
    _finish(store, KIND_RELAY, "relay:done-new", t1)
    store.enqueue(KIND_RELAY, "relay:pending", {}, t1)
    for key in ("relay:done-new", "relay:pending"):
        store.save_relay_state(key, state, t1)
    store.record_relay_sent("wamid.new", "relay:done-new", "update", t1)

    # the return value still counts queue items only
    assert store.purge_finished(t1 - 30 * DAY) == 1
    assert store.get("relay:done-old") is None
    assert store.get_relay_state("relay:done-old") is None
    assert store.get_relay_state("relay:orphan") is None  # no queue item any more
    # a failed item keeps its state, so replaying it never repeats a step that already happened
    assert store.get_relay_state("relay:failed-old") == state
    assert store.get_relay_state("relay:done-new") == state
    assert store.get_relay_state("relay:pending") == state
    assert store.get_relay_sent("wamid.old") is None
    assert store.get_relay_sent("wamid.failed-old") is None  # older than the cutoff
    assert store.get_relay_sent("wamid.new") is not None

    assert store.purge_finished(t1 - 30 * DAY) == 0
    assert sorted(r[0] for r in _sql(path, "SELECT key FROM relay_state")) == [
        "relay:done-new",
        "relay:failed-old",
        "relay:pending",
    ]
    assert _sql(path, "SELECT wamid FROM relay_sent") == [("wamid.new",)]


def test_purge_keeps_message_rows_behaviour(tmp_path, clock: FakeClock):
    store = Store(tmp_path / "q.db")
    _finish(store, KIND_MESSAGE, "wamid.1", clock())
    _finish(store, KIND_MESSAGE, "wamid.2", clock(), failed=True)
    assert store.purge_finished(clock() - 1) == 0
    assert store.purge_finished(clock() + 1) == 1
    assert store.get("wamid.1") is None
    assert store.get("wamid.2") is not None  # failed items are kept for review


# ----------------------------------------------------------------------------- older database files
# The queue schema of the first release (c72eaf0) and of the deployed 570fd03, byte for byte.
_SCHEMA_FIRST_RELEASE = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    first_attempt_at REAL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    finished_at REAL,
    emails_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_status_next ON queue(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""
_SCHEMA_570FD03 = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    first_attempt_at REAL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    finished_at REAL,
    emails_sent INTEGER NOT NULL DEFAULT 0,
    crashes INTEGER NOT NULL DEFAULT 0,
    plan_sig TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status_next ON queue(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def _columns(path: Path, table: str) -> list[tuple[str, str, int, Any, int]]:
    # (name, type, notnull, default, primary key)
    return [(r[1], r[2], r[3], r[4], r[5]) for r in _sql(path, f"PRAGMA table_info({table})")]


@pytest.mark.parametrize("schema", [_SCHEMA_FIRST_RELEASE, _SCHEMA_570FD03], ids=["c72eaf0", "570fd03"])
def test_a_database_from_before_the_feature_gains_the_relay_tables(tmp_path, clock: FakeClock, schema):
    path = tmp_path / "queue.db"
    payload = load_payload("text")
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO queue (kind, key, payload, status, received_at, next_attempt_at)"
        " VALUES (?, ?, ?, 'pending', ?, ?)",
        (KIND_MESSAGE, wamid, json.dumps({"envelope": payload}), clock(), clock()),
    )
    conn.execute("INSERT INTO meta (k, v) VALUES ('crm_index', 'kept')")
    conn.commit()
    conn.close()

    store = Store(path)
    tables = {r[0] for r in _sql(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"queue", "meta", "relay_state", "relay_sent"} <= tables
    assert _columns(path, "relay_state") == [
        ("key", "TEXT", 0, None, 1),
        ("state", "TEXT", 1, None, 0),
        ("updated_at", "REAL", 1, None, 0),
    ]
    assert _columns(path, "relay_sent") == [
        ("wamid", "TEXT", 0, None, 1),
        ("item_key", "TEXT", 1, None, 0),
        ("what", "TEXT", 1, None, 0),
        ("created_at", "REAL", 1, None, 0),
        ("failure_notified", "INTEGER", 1, "0", 0),
    ]
    # what was there before is untouched and still works
    assert store.get_meta("crm_index") == "kept"
    item = store.get(wamid)
    assert item is not None and item.payload == {"envelope": payload} and item.crashes == 0
    # a message received before the upgrade still opens the customer service window
    assert store.recent_inbound_timestamp(PHONE, clock() - DAY) == TEXT_TIMESTAMP
    store.save_relay_state("relay:1", {"steps": {}}, clock())
    assert store.record_relay_sent("wamid.A", "relay:1", "update", clock())
    assert store.mark_relay_failure_notified("wamid.A")
    claimed = store.claim_next(clock())
    assert claimed is not None and claimed.key == wamid
    store.close()

    # opening it again changes nothing
    store = Store(path)
    assert store.get_relay_state("relay:1") == {"steps": {}}
    assert store.get_relay_sent("wamid.A")["failure_notified"] is True
    assert store.get(wamid).status == "processing"
    store.close()
