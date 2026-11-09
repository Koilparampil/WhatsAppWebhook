"""Resume behaviour for split (multi-part) emails and the longer alert retry window."""

from __future__ import annotations

import base64
import hashlib
import os
from email.message import EmailMessage

import httpx
import respx
from app.mail.transports import SendError
from app.store import KIND_ALERT, KIND_MESSAGE, STATUS_FAILED, STATUS_PENDING, Store
from app.worker import Worker

from tests.conftest import load_payload, make_settings
from tests.test_compose import matched_lookup
from tests.test_worker import FakeDirectory


class CountingTransport:
    name = "counting"

    def __init__(self, fail_on_calls: set[int]):
        self.calls = 0
        self.fail_on_calls = fail_on_calls
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        self.calls += 1
        if self.calls in self.fail_on_calls:
            raise SendError("transient smtp failure", retriable=True)
        self.sent.append(message)
        return message["Message-ID"] or ""


def _image_payload_with(data: bytes) -> dict:
    payload = load_payload("image")
    digest = hashlib.sha256(data).digest()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["sha256"] = base64.b64encode(
        digest
    ).decode()
    return payload


@respx.mock
async def test_split_message_resumes_at_the_failed_part(tmp_path, clock):
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
    transport = CountingTransport(fail_on_calls={2})  # part 1 succeeds, part 2 fails once
    worker = Worker(
        settings, store, FakeDirectory(matched_lookup()), transport, httpx.AsyncClient(), clock=clock
    )

    store.enqueue(KIND_MESSAGE, "wamid.IMAGE123", {"envelope": _image_payload_with(data)}, clock())
    await worker.handle(store.claim_next(clock()))
    item = store.get("wamid.IMAGE123")
    assert item.status == STATUS_PENDING
    assert item.emails_sent == 1  # progress persisted
    assert len(transport.sent) == 1

    clock.now = store.next_due_at()
    await worker.handle(store.claim_next(clock()))
    item = store.get("wamid.IMAGE123")
    assert item.status == "done"

    names = [next(m.iter_attachments()).get_filename() for m in transport.sent]
    assert len(names) == len(set(names)) == item.emails_sent  # every part exactly once
    assert names[0].endswith(".001") and names[-1].endswith(f".{len(names):03d}")
    rebuilt = b"".join(next(m.iter_attachments()).get_payload(decode=True) for m in transport.sent)
    assert rebuilt == data


async def test_alert_items_use_the_longer_retry_window(tmp_path, clock):
    settings = make_settings(tmp_path, alert_retry_window_seconds=3600)
    store = Store(settings.store_path)
    transport = CountingTransport(fail_on_calls=set(range(1, 1000)))
    worker = Worker(
        settings, store, FakeDirectory(matched_lookup()), transport, httpx.AsyncClient(), clock=clock
    )

    store.enqueue(KIND_ALERT, "alert:test", {"subject": "s", "body": "b"}, clock())
    start = clock()
    while True:
        item = store.claim_next(clock())
        if item is None:
            clock.now = store.next_due_at()
            continue
        await worker.handle(item)
        current = store.get("alert:test")
        if current.status == STATUS_FAILED:
            break
        if clock() - start > 700:
            # a message would have been dead-lettered by now (600 s window); the alert is still retrying
            assert current.status == STATUS_PENDING
        assert clock() - start < 4000
    assert 3400 <= clock() - start <= 3600
    assert store.list_by_status(STATUS_PENDING) == []  # no alert-about-the-alert


def test_requeue_resets_progress(tmp_path, clock):
    store = Store(tmp_path / "q.db")
    store.enqueue(KIND_MESSAGE, "wamid.1", {}, clock())
    item = store.claim_next(clock())
    store.set_progress(item.id, 3)
    store.mark_failed(item.id, "boom", clock())
    assert store.get("wamid.1").emails_sent == 3
    assert store.requeue("wamid.1", clock()) == 1
    assert store.get("wamid.1").emails_sent == 0
