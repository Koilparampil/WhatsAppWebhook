from __future__ import annotations

import base64
import hashlib
from email.message import EmailMessage

import httpx
import respx
from app.crm.directory import LookupResult, LookupStatus
from app.mail.transports import SendError
from app.store import KIND_ALERT, KIND_MESSAGE, STATUS_FAILED, STATUS_PENDING, Store
from app.worker import Worker, mask_phone

from tests.conftest import FakeClock, load_payload, make_settings, text_payload
from tests.test_compose import matched_lookup


class FakeTransport:
    name = "fake"

    def __init__(self, fail_times: int = 0, retriable: bool = True):
        self.sent: list[EmailMessage] = []
        self.fail_times = fail_times
        self.retriable = retriable

    async def send(self, message: EmailMessage) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SendError("smtp down", retriable=self.retriable)
        self.sent.append(message)
        return message["Message-ID"] or ""


class FakeDirectory:
    def __init__(self, result: LookupResult):
        self.result = result
        self.calls: list[str | None] = []

    async def lookup(self, phone):
        self.calls.append(phone)
        return self.result

    def status(self):
        return {"enabled": True}


def make_worker(tmp_path, clock: FakeClock, transport, lookup=None, **settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    store = Store(settings.store_path)
    directory = FakeDirectory(lookup or matched_lookup())
    worker = Worker(settings, store, directory, transport, httpx.AsyncClient(), clock=clock)
    return worker, store, directory


async def test_success_path_sets_reply_to(tmp_path, clock):
    transport = FakeTransport()
    worker, store, directory = make_worker(tmp_path, clock, transport)
    payload = text_payload()
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, clock())
    item = store.claim_next(clock())
    await worker.handle(item)
    assert store.get(wamid).status == "done"
    assert store.get(wamid).emails_sent == 1
    assert directory.calls == ["16505551234"]
    assert transport.sent[0]["Reply-To"] == "john@acme.com, ops@acme.com"
    assert worker.last_send_ok_at == clock()


async def test_retries_within_window_then_dead_letters_and_alerts(tmp_path, clock):
    transport = FakeTransport(fail_times=1000)
    worker, store, _ = make_worker(tmp_path, clock, transport)
    payload = text_payload()
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, clock())
    start = clock()
    attempts = 0
    while True:
        item = store.claim_next(clock())
        if item is None:
            due = store.next_due_at()
            assert due is not None
            clock.now = due
            continue
        if item.kind == KIND_ALERT:
            break
        attempts += 1
        await worker.handle(item)
        if store.get(wamid).status == STATUS_FAILED:
            break
    failed = store.get(wamid)
    assert failed.status == STATUS_FAILED
    assert attempts == 8  # 0,15,45,105,225,345,465,585 s; the next retry would land after 600 s
    assert clock() - start <= 600
    assert "gave up after 8 attempt(s)" in failed.last_error
    alerts = store.list_by_status(STATUS_PENDING)
    assert len(alerts) == 1 and alerts[0].kind == KIND_ALERT
    transport.fail_times = 0
    alert_item = store.claim_next(clock())
    await worker.handle(alert_item)
    assert transport.sent[-1]["Subject"].startswith(
        "[WhatsApp] ALERT: WhatsApp message could not be forwarded"
    )
    assert wamid in transport.sent[-1].get_content()


async def test_non_retriable_send_error_dead_letters_immediately(tmp_path, clock):
    transport = FakeTransport(fail_times=1, retriable=False)
    worker, store, _ = make_worker(tmp_path, clock, transport, alert_on_dead_letter=False)
    payload = text_payload()
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get(wamid).status == STATUS_FAILED
    assert store.list_by_status(STATUS_PENDING) == []


async def test_permanent_failure_for_corrupt_payload(tmp_path, clock):
    worker, store, _ = make_worker(tmp_path, clock, FakeTransport())
    store.enqueue(
        KIND_MESSAGE, "wamid.X", {"envelope": {"object": "whatsapp_business_account", "entry": []}}, clock()
    )
    await worker.handle(store.claim_next(clock()))
    assert store.get("wamid.X").status == STATUS_FAILED
    assert "permanent" in store.get("wamid.X").last_error


@respx.mock
async def test_media_message_is_downloaded_attached_and_spool_cleaned(tmp_path, clock):
    data = b"\xff\xd8" + b"j" * 3000
    digest = hashlib.sha256(data).digest()
    respx.get("https://graph.facebook.com/v26.0/1385223789055226").mock(
        return_value=httpx.Response(
            200,
            json={"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg", "file_size": len(data)},
        )
    )
    respx.get("https://lookaside.fbsbx.com/x").mock(return_value=httpx.Response(200, content=data))
    transport = FakeTransport()
    worker, store, _ = make_worker(
        tmp_path, clock, transport, lookup=LookupResult(LookupStatus.NO_MATCH, reason="none")
    )
    payload = load_payload("image")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["sha256"] = base64.b64encode(
        digest
    ).decode()
    store.enqueue(KIND_MESSAGE, "wamid.IMAGE123", {"envelope": payload}, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get("wamid.IMAGE123").status == "done"
    att = next(transport.sent[0].iter_attachments())
    assert att.get_payload(decode=True) == data
    assert att.get_filename().endswith(".jpg")
    assert "Reply-To" not in transport.sent[0]
    assert list((tmp_path / "spool").rglob("*")) == []


@respx.mock
async def test_media_failure_is_retried_not_sent_without_attachment(tmp_path, clock):
    respx.get("https://graph.facebook.com/v26.0/1385223789055226").mock(return_value=httpx.Response(503))
    transport = FakeTransport()
    worker, store, _ = make_worker(tmp_path, clock, transport)
    store.enqueue(KIND_MESSAGE, "wamid.IMAGE123", {"envelope": load_payload("image")}, clock())
    await worker.handle(store.claim_next(clock()))
    item = store.get("wamid.IMAGE123")
    assert item.status == STATUS_PENDING and item.attempts == 1
    assert "HTTP 503" in item.last_error
    assert transport.sent == []


def test_mask_phone():
    assert mask_phone("+16505551234") == "+16*****1234"
    assert mask_phone(None) == "unknown"
    assert mask_phone("+16505551234", enabled=False) == "+16505551234"
