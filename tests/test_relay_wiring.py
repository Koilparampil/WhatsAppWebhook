"""How "WhatsApp updates from email" is wired in: the worker's dispatch rules and the app (spec section 7).

The relay processor is replaced by :class:`FakeRelay` (same methods as spec section 13), so these tests do
not depend on its implementation.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app import main as main_module
from app.mail.transports import SendError
from app.main import build_directory, build_relay, create_app
from app.relay.notices import build_relay_alert, to_payload
from app.relay.poller import RelayPoller
from app.store import (
    KIND_ALERT,
    KIND_MESSAGE,
    KIND_NOTE,
    KIND_RELAY,
    KIND_RELAY_STATUS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Store,
)
from app.whatsapp.send import WhatsAppSendError
from app.worker import RELAY_OFF_DELAY_SECONDS, RELAY_OFF_TEXT, Worker
from fastapi.testclient import TestClient

from tests.conftest import FakeClock, load_payload, make_settings, text_payload
from tests.test_app import signed_post
from tests.test_compose import matched_lookup
from tests.test_worker import FakeDirectory, FakeTransport

RELAY_ON: dict[str, Any] = {
    "mail_transport": "graph",
    "graph_sender": "whatsapp@example.com",
    "oauth_tenant_id": "tenant-id",
    "oauth_client_id": "client-id",
    "oauth_client_secret": "client-secret",
    "wa_phone_number_id": "106540352242922",
    "wa_waba_id": "102290129340398",
    "relay_enabled": True,
    "relay_allowed_senders": "chris@example.com",
    "worker_enabled": True,
    "crm_enabled": False,
}
POLLER_STATUS = {
    "enabled": True,
    "mailbox": "whatsapp@example.com",
    "cursor": "2026-11-18T11:06:40Z",
    "last_ok_at": 1795000000.0,
    "last_error": None,
    "failing_since": None,
    "queued": 0,
}
ERROR = {
    "code": 131026,
    "title": "Message undeliverable",
    "message": "Message undeliverable.",
    "error_data": {"details": "Message Undeliverable."},
}


class FakeRelay:
    """Stands in for app.relay.processor.RelayProcessor (spec section 13)."""

    def __init__(self, sent: int = 1, errors: list[BaseException] | None = None):
        self.wakeup: asyncio.Event | None = None
        self.sent = sent
        self.errors = list(errors or [])
        self.processed: list[str] = []
        self.statuses: list[str] = []
        self.dead: list[tuple[str, str]] = []
        self.dead_letter_error: Exception | None = None
        self.flushes = 0

    async def process(self, item) -> int:
        self.processed.append(item.key)
        if self.errors:
            raise self.errors.pop(0)
        return self.sent

    async def process_status(self, item) -> None:
        self.statuses.append(item.key)
        if self.errors:
            raise self.errors.pop(0)

    def on_dead_letter(self, item, error: str) -> None:
        self.dead.append((item.key, error))
        if self.dead_letter_error is not None:
            raise self.dead_letter_error

    def flush_digests(self) -> None:
        self.flushes += 1

    def status(self) -> dict:
        return {"processed": len(self.processed), "statuses": len(self.statuses)}


def relay_worker(tmp_path, clock: FakeClock, relay: FakeRelay | None = None, transport=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    store = Store(settings.store_path)
    transport = transport or FakeTransport()
    worker = Worker(
        settings,
        store,
        FakeDirectory(matched_lookup()),
        transport,
        httpx.AsyncClient(),
        clock=clock,
        relay=relay,
    )
    return worker, store, transport


async def drain(worker: Worker, store: Store, clock: FakeClock, *, until=None, limit: int = 500) -> None:
    """Run the queue (jumping the clock to each retry) until it is empty or ``until()`` is true."""
    for _ in range(limit):
        if until is not None and until():
            return
        item = store.claim_next(clock())
        if item is None:
            due = store.next_due_at()
            if due is None:
                return
            clock.now = max(clock.now, due)
            continue
        await worker.handle(item)
    raise AssertionError("the queue did not drain")


def items_of(store: Store, kind: str) -> list:
    found = []
    for status in (STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE, STATUS_FAILED):
        found.extend(item for item in store.list_by_status(status, limit=1000) if item.kind == kind)
    return found


RELAY_PAYLOAD = {"id": "MSG1", "received": "2026-11-18T15:00:00Z"}


# ----------------------------------------------------------------------------- worker: dispatch
async def test_relay_and_status_items_go_to_the_processor(tmp_path, clock):
    relay = FakeRelay(sent=2)
    worker, store, transport = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    store.enqueue(
        KIND_RELAY_STATUS, "relay_status:wamid.X", {"wamid": "wamid.X", "status": "failed"}, clock()
    )
    await drain(worker, store, clock)
    assert relay.processed == ["relay:MSG1"] and relay.statuses == ["relay_status:wamid.X"]
    done = store.get("relay:MSG1")
    assert done.status == STATUS_DONE and done.emails_sent == 2  # the WhatsApp messages sent
    assert store.get("relay_status:wamid.X").status == STATUS_DONE
    assert transport.sent == []


async def test_note_items_are_sent_exactly_as_stored(tmp_path, clock):
    worker, store, transport = relay_worker(tmp_path, clock)  # notes need no processor
    message = build_relay_alert("Test alert", ["line one", "line two"], worker.settings, now=clock())
    store.enqueue(KIND_NOTE, "note:test", to_payload(message), clock())
    await drain(worker, store, clock)
    (sent,) = transport.sent
    for header in ("From", "To", "Subject", "Message-ID", "Auto-Submitted", "X-WhatsApp-Relay"):
        assert sent[header] == message[header], header
    assert sent.get_body(("plain",)).get_content() == message.get_body(("plain",)).get_content()
    assert sent.get_body(("html",)).get_content() == message.get_body(("html",)).get_content()
    item = store.get("note:test")
    assert item.status == STATUS_DONE and item.emails_sent == 1


async def test_a_note_that_cannot_be_rebuilt_is_given_up_at_once_without_an_alert(tmp_path, clock):
    worker, store, transport = relay_worker(tmp_path, clock)
    store.enqueue(KIND_NOTE, "note:bad", {"to": [], "subject": "x"}, clock())
    await worker.handle(store.claim_next(clock()))
    failed = store.get("note:bad")
    assert failed.status == STATUS_FAILED and failed.last_error.startswith("permanent:")
    assert transport.sent == [] and store.list_by_status(STATUS_PENDING) == []


async def test_note_items_keep_retrying_for_the_alert_window_and_raise_no_alert(tmp_path, clock):
    transport = FakeTransport(fail_times=100_000)
    worker, store, _ = relay_worker(tmp_path, clock, transport=transport, alert_retry_window_seconds=3600)
    message = build_relay_alert("Test alert", ["line"], worker.settings, now=clock())
    store.enqueue(KIND_NOTE, "note:test", to_payload(message), clock())
    start = clock()
    await drain(worker, store, clock, until=lambda: store.get("note:test").status == STATUS_FAILED)
    assert 3400 <= clock() - start <= 3600  # far beyond WORKER_RETRY_WINDOW_SECONDS (600)
    assert "gave up after" in store.get("note:test").last_error
    assert store.list_by_status(STATUS_PENDING) == []  # no alert about a failed note (log only)


async def test_whatsapp_asking_to_wait_delays_only_that_update(tmp_path, clock):
    error = WhatsAppSendError("pair rate limit", code=131056, retriable=True, retry_after=300)
    relay = FakeRelay(errors=[error])
    worker, store, transport = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))
    waiting = store.get("relay:MSG1")
    assert waiting.status == STATUS_PENDING and waiting.next_attempt_at == clock() + 300
    assert worker.paused_until is None  # never the global pause: customers' messages keep flowing
    store.enqueue(KIND_MESSAGE, "wamid.NEXT", {"envelope": text_payload(wamid="wamid.NEXT")}, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("wamid.NEXT").status == STATUS_DONE and len(transport.sent) == 1
    clock.now = waiting.next_attempt_at
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_DONE and relay.processed == ["relay:MSG1"] * 2


async def test_graph_asking_to_wait_still_pauses_everything_as_before(tmp_path, clock):
    relay = FakeRelay(errors=[SendError("Graph returned HTTP 429", retriable=True, retry_after=60)])
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_PENDING
    assert worker.paused_until == clock() + 60  # the existing rule for Graph and mail errors


@pytest.mark.parametrize("relay_mailbox", ["", "WhatsApp@Example.com"])
async def test_graph_throttling_the_sending_mailbox_pauses_everything(tmp_path, clock, relay_mailbox):
    """The relay mailbox is GRAPH_SENDER (owner decision 1): its throttling also holds up sendMail."""
    throttled = SendError("Graph get message returned HTTP 429", retriable=True, retry_after=60)
    relay = FakeRelay(errors=[throttled])
    worker, store, _ = relay_worker(tmp_path, clock, relay, **{**RELAY_ON, "relay_mailbox": relay_mailbox})
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_PENDING
    assert worker.paused_until == clock() + 60


async def test_throttling_of_a_separate_relay_mailbox_holds_up_only_that_update(tmp_path, clock):
    # Graph throttles each app per mailbox: the mailbox the emails are sent from is not affected
    throttled = SendError(
        "Graph download attachment returned HTTP 503: ServiceUnavailable",
        retriable=True,
        status_code=503,
        retry_after=300,
    )
    relay = FakeRelay(errors=[throttled])
    worker, store, transport = relay_worker(
        tmp_path, clock, relay, **{**RELAY_ON, "relay_mailbox": "updates@example.com"}
    )
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))
    waiting = store.get("relay:MSG1")
    assert waiting.status == STATUS_PENDING and waiting.next_attempt_at == clock() + 300  # as long as asked
    assert worker.paused_until is None
    # a customer's message arriving meanwhile is forwarded at once
    store.enqueue(KIND_MESSAGE, "wamid.NEXT", {"envelope": text_payload(wamid="wamid.NEXT")}, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("wamid.NEXT").status == STATUS_DONE and len(transport.sent) == 1
    clock.now = waiting.next_attempt_at
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_DONE and relay.processed == ["relay:MSG1"] * 2

    # Throttling met while sending an email (here the relay's own outcome email) pauses everything as before.
    transport_error = SendError("Graph sendMail returned HTTP 429", retriable=True, retry_after=60)
    worker.transport = SimpleNamespace(name="graph", send=_raise(transport_error))
    message = build_relay_alert("Test alert", ["line"], worker.settings, now=clock())
    store.enqueue(KIND_NOTE, "note:test", to_payload(message), clock())
    await worker.handle(store.claim_next(clock()))
    assert worker.paused_until == clock() + 60


def _raise(error: BaseException):
    async def send(message) -> str:
        raise error

    return send


async def test_relay_items_never_make_a_failing_mail_service_look_healthy(tmp_path, clock):
    relay = FakeRelay(sent=1)  # one WhatsApp message, and no email
    worker, store, _ = relay_worker(tmp_path, clock, relay, transport=FakeTransport(fail_times=1))
    store.enqueue(KIND_MESSAGE, "wamid.A", {"envelope": text_payload(wamid="wamid.A")}, clock())
    await worker.handle(store.claim_next(clock()))  # the email cannot be sent: tried again later
    assert worker.status()["last_send_ok_at"] is None and worker.status()["last_error"] == "smtp down"

    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    store.enqueue(
        KIND_RELAY_STATUS, "relay_status:wamid.X", {"wamid": "wamid.X", "status": "failed"}, clock()
    )
    for key in ("relay:MSG1", "relay_status:wamid.X"):
        item = store.claim_next(clock())
        assert item.key == key
        await worker.handle(item)
        assert store.get(key).status == STATUS_DONE
    status = worker.status()  # /healthz still tells the truth about the emails
    assert status["last_send_ok_at"] is None and status["last_error"] == "smtp down"

    # An email that goes out is what counts, the feature's own emails (notes) included.
    message = build_relay_alert("Test alert", ["line"], worker.settings, now=clock())
    store.enqueue(KIND_NOTE, "note:test", to_payload(message), clock())
    await worker.handle(store.claim_next(clock()))
    status = worker.status()
    assert status["last_send_ok_at"] == clock() and status["last_error"] is None


async def test_a_whatsapp_refusal_is_given_up_and_the_processor_tells_the_employee(tmp_path, clock):
    relay = FakeRelay(errors=[WhatsAppSendError("invalid parameter", code=131009)])
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    worker.wakeup.clear()
    await worker.handle(store.claim_next(clock()))
    failed = store.get("relay:MSG1")
    assert failed.status == STATUS_FAILED and failed.last_error == "not retriable: invalid parameter"
    assert relay.dead == [("relay:MSG1", "not retriable: invalid parameter")]
    assert worker.wakeup.is_set()
    assert items_of(store, KIND_ALERT) == []  # the processor writes the notices, not the worker


async def test_an_unclear_send_is_never_tried_again(tmp_path, clock):
    relay = FakeRelay(errors=[WhatsAppSendError("no answer from WhatsApp", uncertain=True)])
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await drain(worker, store, clock)
    assert store.get("relay:MSG1").status == STATUS_FAILED
    assert relay.processed == ["relay:MSG1"]  # once
    assert len(relay.dead) == 1


async def test_passing_whatsapp_errors_are_retried_within_the_window_then_given_up(tmp_path, clock):
    errors = [WhatsAppSendError("rate limit", code=130429, retriable=True) for _ in range(50)]
    relay = FakeRelay(errors=errors)
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    start = clock()
    await drain(worker, store, clock)
    failed = store.get("relay:MSG1")
    assert failed.status == STATUS_FAILED and "gave up after 8 attempt(s)" in failed.last_error
    assert clock() - start <= 600 and len(relay.processed) == 8
    assert [key for key, _ in relay.dead] == ["relay:MSG1"]


async def test_a_failing_dead_letter_notice_does_not_break_the_worker(tmp_path, clock):
    relay = FakeRelay(errors=[WhatsAppSendError("refused", code=131009)])
    relay.dead_letter_error = RuntimeError("database is locked")
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_FAILED and len(relay.dead) == 1


@pytest.mark.parametrize(("locked_writes", "tries"), [(1, 2), (4, 4)])
async def test_the_processor_hears_of_a_given_up_update_before_it_is_marked_failed(
    tmp_path, clock, monkeypatch, locked_writes, tries
):
    """The processor's emails are queued before the dead letter is recorded: a database locked from then on
    cannot leave the update unreported, and a locked one makes the worker repeat the whole dead letter
    (the processor queues nothing twice)."""
    relay = FakeRelay(errors=[WhatsAppSendError("refused", code=131009)])
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    worker.outcome_write_delays = (0.0, 0.0, 0.0)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    seen: list[str] = []  # the item's queue status each time the processor is told
    on_dead_letter = relay.on_dead_letter

    def told(item, error: str) -> None:
        seen.append(store.get(item.key).status)
        on_dead_letter(item, error)

    relay.on_dead_letter = told  # type: ignore[method-assign]
    mark_failed = store.mark_failed
    busy = [sqlite3.OperationalError("database is locked") for _ in range(locked_writes)]

    def locked(item_id: int, error: str, now: float | None = None) -> None:
        if busy:
            raise busy.pop()
        mark_failed(item_id, error, now)

    monkeypatch.setattr(store, "mark_failed", locked)
    item = store.claim_next(clock())
    if locked_writes < tries:
        await worker.handle(item)
        assert store.get("relay:MSG1").status == STATUS_FAILED
    else:  # locked for longer than the worker waits: its loop re-queues the item (see Worker.run)
        with pytest.raises(sqlite3.OperationalError):
            await worker.handle(item)
        assert store.get("relay:MSG1").status == STATUS_PROCESSING
    assert seen == [STATUS_PROCESSING] * tries  # each time before the item was marked failed
    assert relay.dead == [("relay:MSG1", "not retriable: refused")] * tries


async def test_a_relay_item_that_keeps_crashing_the_service_is_reported_to_the_processor(tmp_path, clock):
    relay = FakeRelay()
    worker, store, _ = relay_worker(tmp_path, clock, relay)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    for _ in range(worker.settings.worker_max_crash_attempts - 1):
        item = store.claim_next(clock())
        store.mark_interrupted(item.id, clock(), "crash")
    store.claim_next(clock())  # in processing when the service dies once more
    worker.recover_interrupted()
    assert store.get("relay:MSG1").status == STATUS_FAILED
    assert relay.dead and relay.dead[0][0] == "relay:MSG1" and "interrupted" in relay.dead[0][1]
    assert relay.processed == []


async def test_relay_items_that_keep_crashing_wait_while_the_feature_is_off(tmp_path, clock):
    """Given up without the processor, the sender would never hear what became of the update, and a replay
    could still send it: such an item waits like any relay item while the feature is off."""
    worker, store, _ = relay_worker(tmp_path, clock)  # restarted with RELAY_ENABLED=false: no processor
    keys = {"relay:MSG1", "relay_status:wamid.X"}
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    store.enqueue(
        KIND_RELAY_STATUS, "relay_status:wamid.X", {"wamid": "wamid.X", "status": "failed"}, clock()
    )
    cap = worker.settings.worker_max_crash_attempts
    for crash in range(1, cap + 1):
        claimed = [store.claim_next(clock()) for _ in keys]
        assert {item.key for item in claimed} == keys
        if crash < cap:  # the last time, both are still in processing when the service dies
            for item in claimed:
                store.mark_interrupted(item.id, clock(), "crash")
    worker.recover_interrupted()
    for key in keys:
        waiting = store.get(key)
        assert waiting.status == STATUS_PENDING and waiting.last_error == RELAY_OFF_TEXT
        assert waiting.next_attempt_at == clock() + RELAY_OFF_DELAY_SECONDS
        assert waiting.crashes == cap - 1  # not counted: it gets its last attempt once the feature is on
    assert items_of(store, KIND_ALERT) == []

    # Switched on again: handled at last, and never given up for the crashes while the feature was off.
    relay = FakeRelay()
    clock.now = store.next_due_at()
    restarted = Worker(
        worker.settings,
        store,
        FakeDirectory(matched_lookup()),
        FakeTransport(),
        httpx.AsyncClient(),
        clock=clock,
        relay=relay,
    )
    await drain(restarted, store, clock)
    assert all(store.get(key).status == STATUS_DONE for key in keys)
    assert relay.processed == ["relay:MSG1"] and relay.statuses == ["relay_status:wamid.X"]
    assert relay.dead == []


async def test_relay_items_wait_while_the_feature_is_off_and_are_never_given_up(tmp_path, clock):
    worker, store, _ = relay_worker(tmp_path, clock)  # RELAY_ENABLED=false: no processor
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    store.enqueue(KIND_RELAY_STATUS, "relay_status:wamid.X", {"wamid": "wamid.X"}, clock())
    start = clock()
    for _ in range(2 * 72):  # three days, two items looked at every hour
        item = store.claim_next(clock())
        if item is None:
            clock.now = store.next_due_at()
            item = store.claim_next(clock())
        await worker.handle(item)
        waiting = store.get(item.key)
        assert waiting.status == STATUS_PENDING and waiting.last_error == RELAY_OFF_TEXT
        assert waiting.next_attempt_at == clock() + RELAY_OFF_DELAY_SECONDS
    assert clock() - start >= 71 * 3600
    assert items_of(store, KIND_ALERT) == [] and worker.paused_until is None

    # Switched on again (a restart with RELAY_ENABLED=true): handled, with a fresh retry window, so a
    # passing error does not give up an update whose first attempt was three days ago.
    relay = FakeRelay(errors=[WhatsAppSendError("throttled", code=4, retriable=True)])
    clock.now = store.next_due_at()
    restarted = Worker(
        worker.settings,
        store,
        FakeDirectory(matched_lookup()),
        FakeTransport(),
        httpx.AsyncClient(),
        clock=clock,
        relay=relay,
    )
    await drain(restarted, store, clock)
    assert store.get("relay:MSG1").status == STATUS_DONE
    assert store.get("relay_status:wamid.X").status == STATUS_DONE
    assert relay.processed == ["relay:MSG1", "relay:MSG1"] and relay.statuses == ["relay_status:wamid.X"]
    assert relay.dead == []


async def test_attaching_the_processor_later_also_starts_a_fresh_retry_window(tmp_path, clock):
    worker, store, _ = relay_worker(tmp_path, clock)
    store.enqueue(KIND_RELAY, "relay:MSG1", RELAY_PAYLOAD, clock())
    await worker.handle(store.claim_next(clock()))  # waits: first attempt now
    clock.advance(RELAY_OFF_DELAY_SECONDS)
    worker.relay = FakeRelay(errors=[WhatsAppSendError("throttled", code=4, retriable=True)])
    await worker.handle(store.claim_next(clock()))
    assert store.get("relay:MSG1").status == STATUS_PENDING  # retried, not given up
    worker.relay = None
    assert worker.relay is None


# ----------------------------------------------------------------------------- the app
def status_payload(wamid: str, status: str, errors: list | None = None) -> dict:
    payload = copy.deepcopy(load_payload("statuses"))
    entry: dict[str, Any] = {
        "id": wamid,
        "status": status,
        "timestamp": "1750030073",
        "recipient_id": "16505551234",
    }
    if errors is not None:
        entry["errors"] = errors
    payload["entry"][0]["changes"][0]["value"]["statuses"] = [entry]
    return payload


class FakePoller:
    """Stands in for RelayPoller (spec section 13)."""

    def __init__(self) -> None:
        self.wakeup: asyncio.Event | None = None
        self.runs = 0
        self.stopped = False

    async def run(self, stop: asyncio.Event) -> None:
        self.runs += 1
        await stop.wait()
        self.stopped = True

    async def poll_once(self) -> int:
        return 0

    def status(self) -> dict:
        return dict(POLLER_STATUS)


async def task_names() -> set[str]:
    return {task.get_name() for task in asyncio.all_tasks()}


def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def relay_app_settings(tmp_path, **overrides: Any):
    return make_settings(tmp_path, **{**RELAY_ON, **overrides})


@pytest.fixture
def fakes(monkeypatch):
    """build_relay hands out fakes; the mail transport is a fake too (the worker runs in these tests)."""
    found = SimpleNamespace(relay=FakeRelay(), poller=FakePoller(), calls=[], transport=FakeTransport())

    def fake_build(settings, store, directory, http, *, wakeup=None):
        found.calls.append(
            {"settings": settings, "store": store, "directory": directory, "http": http, "wakeup": wakeup}
        )
        return found.relay, found.poller

    monkeypatch.setattr(main_module, "build_relay", fake_build)
    monkeypatch.setattr(main_module, "build_transport", lambda settings, http: found.transport)
    return found


@pytest.fixture
def no_relay(monkeypatch):
    """build_relay must not even be called; the mail transport is a fake."""

    def forbidden(*args, **kwargs):
        raise AssertionError("build_relay must not be called")

    transport = FakeTransport()
    monkeypatch.setattr(main_module, "build_relay", forbidden)
    monkeypatch.setattr(main_module, "build_transport", lambda settings, http: transport)
    return transport


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def startup_line(app) -> str:
    """The "startup complete" log line of ``app``'s lifespan."""
    handler = Lines()
    logger = logging.getLogger("app")  # create_app has configured logging already
    logger.addHandler(handler)
    try:
        with TestClient(app):
            pass
    finally:
        logger.removeHandler(handler)
    (line,) = [line for line in handler.lines if line.startswith("startup complete")]
    return line


def test_feature_off_starts_no_relay_and_the_webhook_answers_as_before(tmp_path, no_relay):
    settings = make_settings(tmp_path, crm_enabled=False, worker_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        store: Store = app.state.store
        assert app.state.relay is None and app.state.relay_poller is None and app.state.relay_task is None
        assert app.state.worker.relay is None
        assert "relay-poller" not in client.portal.call(task_names)
        store.record_relay_sent("wamid.REL1", "relay:MSG1", "update", time.time())
        first = signed_post(client, settings, text_payload())
        assert first.status_code == 200 and first.json() == {"status": "ok", "queued": 1, "duplicates": 0}
        failed = signed_post(client, settings, status_payload("wamid.REL1", "failed", [ERROR]))
        assert failed.status_code == 200 and failed.json() == {"status": "ok", "queued": 0, "duplicates": 0}
        assert items_of(store, KIND_RELAY_STATUS) == []
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["relay"] == {"enabled": False}


def test_feature_on_starts_the_poller_and_wires_the_processor_into_the_worker(tmp_path, fakes):
    settings = relay_app_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        worker: Worker = app.state.worker
        assert app.state.relay is fakes.relay and app.state.relay_poller is fakes.poller
        assert worker.relay is fakes.relay
        assert fakes.relay.wakeup is worker.wakeup and fakes.poller.wakeup is worker.wakeup
        (call,) = fakes.calls
        assert call["settings"] is settings and call["store"] is app.state.store
        assert call["directory"] is app.state.directory and call["wakeup"] is worker.wakeup
        assert isinstance(call["http"], httpx.AsyncClient)
        assert app.state.relay_task.get_name() == "relay-poller"
        wait_until(lambda: fakes.poller.runs == 1)
        assert "relay-poller" in client.portal.call(task_names)
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["ok"] is True
        assert health.json()["relay"] == {
            "enabled": True,
            "dry_run": True,
            "test_mode": False,
            "last_poll_ok_at": 1795000000.0,
            "last_error": None,
            "failing_since": None,
            "poller_alive": True,
            "poller": POLLER_STATUS,
            "processor": {"processed": 0, "statuses": 0},
        }
    assert fakes.poller.stopped  # shutdown stops the poller


def test_healthz_shows_live_and_test_mode_and_survives_a_broken_status(tmp_path, fakes):
    settings = relay_app_settings(tmp_path, relay_dry_run=False, relay_test_numbers="+1 650 555 1234")

    def broken() -> dict:
        raise RuntimeError("status broke")

    fakes.relay.status = broken
    app = create_app(settings)
    with TestClient(app) as client:
        relay = client.get("/healthz").json()["relay"]
        assert relay["enabled"] is True and relay["dry_run"] is False and relay["test_mode"] is True
        assert relay["processor"] == {"error": "status not available: RuntimeError('status broke')"}


def test_each_failed_report_is_queued_once_unless_its_sender_was_told_already(tmp_path, fakes):
    settings = relay_app_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        store: Store = app.state.store
        now = time.time()
        store.record_relay_sent("wamid.REL1", "relay:MSG1", "update", now)
        store.record_relay_sent("wamid.TOLD", "relay:MSG2", "file:form.pdf", now)
        assert store.mark_relay_failure_notified("wamid.TOLD")
        answers = [
            signed_post(client, settings, status_payload("wamid.REL1", "failed", [ERROR])),
            signed_post(client, settings, status_payload("wamid.REL1", "failed", [ERROR])),  # Meta repeats
            signed_post(client, settings, status_payload("wamid.REL1", "delivered")),
            # Not recorded (yet): the report may have overtaken WhatsApp's answer to the send, so it is
            # kept as well; the processor ignores it if the relay did not send that message.
            signed_post(client, settings, status_payload("wamid.EARLY", "failed", [ERROR])),
            signed_post(client, settings, status_payload("wamid.EARLY", "failed", [ERROR])),
            signed_post(client, settings, status_payload("wamid.TOLD", "failed", [ERROR])),  # already told
        ]
        for answer in answers:  # the answer to Meta is unchanged
            assert answer.status_code == 200
            assert answer.json() == {"status": "ok", "queued": 0, "duplicates": 0}
        item, early = sorted(items_of(store, KIND_RELAY_STATUS), key=lambda queued: queued.id)
        assert item.key == "relay_status:wamid.REL1" and early.key == "relay_status:wamid.EARLY"
        assert item.payload == {
            "wamid": "wamid.REL1",
            "status": "failed",
            "timestamp": 1750030073,
            "recipient_id": "16505551234",
            "errors": [
                {
                    "code": 131026,
                    "title": "Message undeliverable",
                    "message": "Message undeliverable.",
                    "details": "Message Undeliverable.",
                }
            ],
        }
        # the worker hands each to the processor, once
        wait_until(lambda: all(store.get(queued.key).status == STATUS_DONE for queued in (item, early)))
        assert fakes.relay.statuses == ["relay_status:wamid.REL1", "relay_status:wamid.EARLY"]


def test_the_webhook_takes_the_messages_even_if_relay_bookkeeping_breaks(tmp_path, fakes, monkeypatch):
    settings = relay_app_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:

        def broken(wamid):
            raise RuntimeError("disk full")

        monkeypatch.setattr(app.state.store, "get_relay_sent", broken)
        payload = text_payload()
        payload["entry"][0]["changes"][0]["value"]["statuses"] = status_payload("wamid.REL1", "failed")[
            "entry"
        ][0]["changes"][0]["value"]["statuses"]
        answer = signed_post(client, settings, payload)
        assert answer.status_code == 200 and answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}
        assert app.state.store.get(payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"])


def test_a_relay_that_cannot_be_built_leaves_the_forwarder_running(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("processor broken")

    transport = FakeTransport()
    monkeypatch.setattr(main_module, "build_relay", boom)
    monkeypatch.setattr(main_module, "build_transport", lambda settings, http: transport)
    settings = relay_app_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.relay is None and app.state.relay_task is None and app.state.worker.relay is None
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["relay"] == {
            "enabled": False,
            "error": "could not be started: RuntimeError('processor broken')",
        }
        answer = signed_post(client, settings, text_payload())
        assert answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}


def test_relay_needs_the_worker(tmp_path, no_relay):
    settings = relay_app_settings(tmp_path, worker_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.relay is None and app.state.relay_task is None
        assert client.get("/healthz").json()["relay"] == {"enabled": False}


def test_the_startup_line_says_what_the_relay_does(tmp_path, fakes, monkeypatch):
    on = create_app(relay_app_settings(tmp_path / "on"))
    assert "relay=on (dry run, mailbox whatsapp@example.com)" in startup_line(on)
    live = create_app(
        relay_app_settings(tmp_path / "live", relay_dry_run=False, relay_test_numbers="6505551234")
    )
    assert "relay=on (live, test mode, mailbox whatsapp@example.com)" in startup_line(live)
    off = create_app(make_settings(tmp_path / "off", crm_enabled=False, worker_enabled=True))
    assert "relay=off" in startup_line(off)

    def boom(*args, **kwargs):
        raise RuntimeError("processor broken")

    monkeypatch.setattr(main_module, "build_relay", boom)
    broken = create_app(relay_app_settings(tmp_path / "broken"))
    assert "relay=failed to start" in startup_line(broken)


async def test_build_relay_needs_both_the_feature_and_the_worker(tmp_path):
    async with httpx.AsyncClient() as http:
        for overrides in ({"relay_enabled": False}, {"worker_enabled": False}):
            settings = relay_app_settings(tmp_path, **overrides)
            store = Store(settings.store_path)
            assert build_relay(settings, store, build_directory(settings, http), http) is None


async def test_build_relay_builds_the_real_parts(tmp_path):
    processor = pytest.importorskip("app.relay.processor")
    settings = relay_app_settings(tmp_path)
    store = Store(settings.store_path)
    wakeup = asyncio.Event()
    async with httpx.AsyncClient() as http:
        parts = build_relay(settings, store, build_directory(settings, http), http, wakeup=wakeup)
        assert parts is not None
        relay, poller = parts
        assert isinstance(relay, processor.RelayProcessor) and isinstance(poller, RelayPoller)
        assert relay.wakeup is wakeup and poller.wakeup is wakeup
        assert poller.on_round == relay.flush_digests
        assert poller.store is store and poller.mailbox.mailbox == "whatsapp@example.com"


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"relay_enabled": "ture"}, "RELAY_ENABLED must be true or false, not 'ture'"),
        (
            {"relay_enabled": False, "relay_poll_seconds": "30s"},
            "RELAY_POLL_SECONDS must be a number of seconds",
        ),
    ],
)
def test_an_unreadable_relay_value_is_reported_although_the_feature_is_off(
    tmp_path, no_relay, overrides, problem
):
    """RELAY_ENABLED=ture keeps the feature off; the log and /healthz say why, so nobody believes it is on."""
    settings = make_settings(tmp_path, crm_enabled=False, worker_enabled=True, **overrides)
    assert settings.relay_enabled is False
    assert "relay=off (a RELAY_* setting cannot be read)" in startup_line(create_app(settings))
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.relay is None and app.state.worker.relay is None
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["ok"] is True
        relay = health.json()["relay"]
        assert relay["enabled"] is False
        assert relay["error"].startswith("not started, settings: ") and problem in relay["error"]
        answer = signed_post(client, settings, text_payload())
        assert answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}


def test_bad_relay_settings_leave_only_the_relay_off_and_the_forwarder_running(tmp_path, no_relay):
    """A mistake in a RELAY_* setting must never stop the service (and with it the forwarder)."""
    settings = relay_app_settings(tmp_path, relay_allowed_senders="", relay_poll_seconds=1)
    assert settings.relay_problems()  # the settings load; only the relay is affected
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.relay is None and app.state.worker.relay is None
        health = client.get("/healthz")
        assert health.status_code == 200
        relay = health.json()["relay"]
        assert relay["enabled"] is False
        assert relay["error"].startswith("not started, settings: ")
        assert "RELAY_ALLOWED_SENDERS must list at least one address" in relay["error"]
        assert "RELAY_POLL_SECONDS must be at least 10" in relay["error"]
        answer = signed_post(client, settings, text_payload())
        assert answer.json() == {"status": "ok", "queued": 1, "duplicates": 0}
