"""Regression tests for the issues found in the adversarial code review."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import httpx
import pytest
import respx
from app.crm.phones import MatchCandidate, rank_candidates, record_from_api
from app.mail.compose import build_emails
from app.mail.transports import GraphTransport, OAuthTokenProvider, SendError
from app.main import configure_logging, create_app
from app.store import KIND_ALERT, KIND_MESSAGE, STATUS_FAILED, STATUS_PENDING, STATUS_PROCESSING, Store
from app.whatsapp.media import MediaDownloadError, download_media
from app.whatsapp.parser import MediaRef, parse_webhook
from app.worker import Worker
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import FakeClock, make_settings, text_payload
from tests.test_compose import matched_lookup
from tests.test_directory import CUSTOMERS, FakeClient, make_directory
from tests.test_transports import graph_settings, sample_message
from tests.test_worker import FakeDirectory, FakeTransport


# ----------------------------------------------------------------------------- headers
def test_unicode_line_separators_in_display_name_do_not_break_the_email(settings):
    msg = parse_webhook(text_payload(name="Evil Name\x0bHere\x00!")).messages[0]  # noqa: RUF001
    m = build_emails(msg, matched_lookup(), None, settings)[0].message
    assert m["From"] == "Evil Name Here! via WhatsApp <whatsapp@example.com>"
    assert " " not in m["Subject"] and "\x0b" not in m["Subject"]  # noqa: RUF001
    assert m.as_bytes()  # serialises without a header error


# ----------------------------------------------------------------------------- media id
@respx.mock
async def test_malformed_media_id_is_rejected_before_any_request(tmp_path):
    route = respx.get(url__regex=r"https://graph\.facebook\.com/.*").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        with pytest.raises(MediaDownloadError) as exc:
            await download_media(
                http,
                media=MediaRef(kind="image", id="../../me?fields=id"),
                wamid="w",
                access_token="tok",
                graph_base_url="https://graph.facebook.com",
                api_version="v27.0",
                dest_dir=tmp_path,
            )
    assert exc.value.retriable is False
    assert route.call_count == 0


# ----------------------------------------------------------------------------- CRM dates
def test_dotnet_default_date_does_not_crash_ranking():
    rec = record_from_api(
        "Customer",
        {"id": 7, "mobile": "650-555-1234", "email1": "x@example.com", "updatedDate": "0001-01-01T00:00:00"},
        "US",
    )
    assert rec is not None and rec.updated_at is None
    ranked = rank_candidates([MatchCandidate(rec, "mobile", "650-555-1234", exact=True)], ["Customer"])
    assert ranked[0].record.record_id == "7"
    rec2 = record_from_api(
        "Customer", {"id": 8, "mobile": "650-555-1234", "updatedDate": "2026-10-01T12:00:00"}, "US"
    )
    assert rec2 is not None and rec2.updated_at is not None and rec2.updated_at.tzinfo is not None


async def test_lookup_crash_degrades_to_lookup_error(tmp_path, clock):
    class ExplodingDirectory:
        entities = ("Customer",)

        async def lookup(self, phone):
            raise RuntimeError("boom")

        def index_age(self):
            return 12.0

    settings = make_settings(tmp_path)
    store = Store(settings.store_path)
    transport = FakeTransport()
    worker = Worker(settings, store, ExplodingDirectory(), transport, httpx.AsyncClient(), clock=clock)
    payload = text_payload()
    wamid = payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    store.enqueue(KIND_MESSAGE, wamid, {"envelope": payload}, clock())
    await worker.handle(store.claim_next(clock()))
    assert store.get(wamid).status == "done"
    body = transport.sent[0].get_body(("plain",)).get_content()
    assert "Lookup failed: lookup crashed: RuntimeError('boom')" in body
    assert "Reply-To" not in transport.sent[0]


# ----------------------------------------------------------------------------- worker resilience
def _worker(tmp_path, clock, transport=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    store = Store(settings.store_path)
    worker = Worker(
        settings,
        store,
        FakeDirectory(matched_lookup()),
        transport or FakeTransport(),
        httpx.AsyncClient(),
        clock=clock,
    )
    worker.error_pause_seconds = 0.05
    worker.outcome_write_delays = (0.01, 0.01, 0.01)
    return worker, store


async def _run_until(worker, store, predicate, limit_seconds=5.0):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    while loop.time() < deadline and not predicate():  # noqa: ASYNC110 - polling store state in a test
        await asyncio.sleep(0.02)
    stop.set()
    worker.wakeup.set()
    await asyncio.wait_for(task, timeout=5)


async def test_busy_database_on_outcome_write_is_retried_without_a_loop_error(tmp_path):
    worker, store = _worker(tmp_path, FakeClock(), worker_poll_seconds=0.05)
    original = store.mark_done
    calls = {"n": 0}

    def flaky_mark_done(item_id, emails_sent=0, now=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return original(item_id, emails_sent, now)

    store.mark_done = flaky_mark_done  # type: ignore[method-assign]
    store.enqueue(KIND_MESSAGE, "wamid.A", {"envelope": text_payload(wamid="wamid.A")}, FakeClock()())
    await _run_until(worker, store, lambda: store.get("wamid.A").status == "done")
    assert store.get("wamid.A").status == "done"
    assert worker.loop_errors == 0
    assert len(worker.transport.sent) == 1  # no duplicate email


async def test_persistent_store_error_is_survived_and_the_stuck_row_requeued(tmp_path):
    worker, store = _worker(tmp_path, FakeClock(), worker_poll_seconds=0.05)
    original = store.mark_done
    calls = {"n": 0}

    def broken_then_ok(item_id, emails_sent=0, now=None):
        calls["n"] += 1
        if calls["n"] <= 4:  # more failures than the write retries
            raise sqlite3.OperationalError("disk I/O error")
        return original(item_id, emails_sent, now)

    store.mark_done = broken_then_ok  # type: ignore[method-assign]
    clock = worker.clock
    store.enqueue(KIND_MESSAGE, "wamid.A", {"envelope": text_payload(wamid="wamid.A")}, clock())
    store.enqueue(KIND_MESSAGE, "wamid.B", {"envelope": text_payload(wamid="wamid.B")}, clock())
    await _run_until(
        worker, store, lambda: store.get("wamid.A").status == "done" and store.get("wamid.B").status == "done"
    )
    assert worker.loop_errors == 1
    assert store.get("wamid.A").status == "done"  # re-queued from 'processing', not stuck
    assert store.get("wamid.B").status == "done"
    assert store.list_by_status(STATUS_PROCESSING) == []


def test_recover_interrupted_counts_crashes_not_attempts(tmp_path, clock):
    worker, store = _worker(tmp_path, clock, worker_max_crash_attempts=3)
    store.enqueue(KIND_MESSAGE, "wamid.flaky", {"envelope": text_payload(wamid="wamid.flaky")}, clock())
    # two ordinary failures, then the process dies during the third attempt
    for _ in range(3):
        item = store.claim_next(clock())
        assert item is not None
        if item.attempts < 3:
            store.mark_retry(item.id, clock(), "451 try later")
    worker.recover_interrupted()
    item = store.get("wamid.flaky")
    assert item.status == STATUS_PENDING and item.crashes == 1 and item.next_attempt_at > clock()
    assert "stopped unexpectedly" in item.last_error

    # the same message kills the process two more times -> dead letter with one alert
    for _ in range(2):
        clock.now = store.next_due_at()
        assert store.claim_next(clock()).key == "wamid.flaky"
        worker.recover_interrupted()
    item = store.get("wamid.flaky")
    assert item.status == STATUS_FAILED and item.crashes == 3
    assert "3 interrupted attempt(s)" in item.last_error
    alerts = [i for i in store.list_by_status(STATUS_PENDING) if i.kind == KIND_ALERT]
    assert len(alerts) == 1 and "wamid.flaky" in alerts[0].payload["subject"]
    assert store.requeue("wamid.flaky", clock()) == 1
    assert store.get("wamid.flaky").crashes == 0


async def test_graceful_shutdown_requeues_without_counting_a_crash(tmp_path, clock):
    class HangingTransport(FakeTransport):
        async def send(self, message):
            await asyncio.sleep(3600)

    worker, store = _worker(tmp_path, clock, transport=HangingTransport())
    store.enqueue(KIND_MESSAGE, "wamid.slow", {"envelope": text_payload(wamid="wamid.slow")}, clock())
    item = store.claim_next(clock())
    task = asyncio.create_task(worker.handle(item))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = store.get("wamid.slow")
    assert row.status == STATUS_PENDING and row.crashes == 0
    assert "service stopped" in row.last_error
    worker.recover_interrupted()  # nothing left in processing, nothing counted
    assert store.get("wamid.slow").crashes == 0


def test_store_migrates_an_older_database(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
        " key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
        " attempts INTEGER NOT NULL DEFAULT 0, received_at REAL NOT NULL, first_attempt_at REAL,"
        " next_attempt_at REAL NOT NULL, last_error TEXT, finished_at REAL,"
        " emails_sent INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO queue (kind, key, payload, received_at, next_attempt_at)"
        " VALUES ('message', 'k', '{}', 1, 1);"
    )
    conn.commit()
    conn.close()
    store = Store(db)
    item = store.get("k")
    assert item.crashes == 0 and item.plan_sig is None


def test_clean_spool_only_cleans_a_marked_directory(tmp_path, clock):
    worker, _store = _worker(tmp_path, clock)
    spool = worker.settings.spool_dir
    assert worker.clean_spool() == 0  # creates the directory and its marker
    assert (spool / ".whatsapp-webhook-spool").exists()
    (spool / "wamid_X").mkdir()
    (spool / "wamid_X" / "123.part").write_bytes(b"partial")
    (spool / "loose.bin").write_bytes(b"x")
    assert worker.clean_spool() == 2
    assert [p.name for p in spool.iterdir()] == [".whatsapp-webhook-spool"]

    other = tmp_path / "not-a-spool"
    other.mkdir()
    (other / "important.txt").write_text("keep me")
    worker2, _ = _worker(tmp_path / "w2", clock, spool_dir=other)
    assert worker2.clean_spool() == 0
    assert (other / "important.txt").exists()


def test_spool_and_store_paths_must_not_overlap(tmp_path):
    with pytest.raises(ValidationError, match="STORE_PATH must not be inside SPOOL_DIR"):
        make_settings(tmp_path, spool_dir=tmp_path / "data", store_path=tmp_path / "data" / "spool" / "q.db")
    with pytest.raises(ValidationError, match="SPOOL_DIR must be its own directory"):
        make_settings(tmp_path, spool_dir=tmp_path / "data", store_path=tmp_path / "data" / "q.db")


def test_smtp_credentials_require_encryption(tmp_path):
    with pytest.raises(ValidationError, match="unencrypted"):
        make_settings(
            tmp_path, smtp_security="none", smtp_auth="password", smtp_username="u", smtp_password="p"
        )
    make_settings(tmp_path, smtp_security="none", smtp_auth="none")  # anonymous relay is allowed


# ----------------------------------------------------------------------------- Graph size threshold
@respx.mock
async def test_graph_uses_draft_path_when_encoded_mime_would_exceed_the_limit(tmp_path):
    settings = graph_settings(tmp_path)
    respx.post("https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    send_mime = respx.post("https://graph.microsoft.com/v1.0/users/sender@example.com/sendMail").mock(
        return_value=httpx.Response(202)
    )
    draft = respx.post("https://graph.microsoft.com/v1.0/users/sender@example.com/messages").mock(
        return_value=httpx.Response(201, json={"id": "D1"})
    )
    respx.post(url__regex=r".*/messages/D1/attachments$").mock(return_value=httpx.Response(201, json={}))
    respx.post(url__regex=r".*/messages/D1/send$").mock(return_value=httpx.Response(202))
    message = sample_message()
    del message["Content-Type"]  # rebuild with a 2.7 MB attachment: raw MIME ~3.7 MB, encoded ~4.9 MB
    message = sample_message()
    message.add_attachment(b"x" * 2_700_000, maintype="application", subtype="pdf", filename="big.pdf")
    async with httpx.AsyncClient() as http:
        transport = GraphTransport(http, settings, OAuthTokenProvider(http, settings))
        await transport.send(message)
    assert send_mime.call_count == 0 and draft.call_count == 1


@respx.mock
async def test_graph_413_on_sendmail_falls_back_to_draft(tmp_path):
    settings = graph_settings(tmp_path)
    respx.post("https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    send_mime = respx.post("https://graph.microsoft.com/v1.0/users/sender@example.com/sendMail").mock(
        return_value=httpx.Response(413, json={"error": {"message": "too big"}})
    )
    draft = respx.post("https://graph.microsoft.com/v1.0/users/sender@example.com/messages").mock(
        return_value=httpx.Response(201, json={"id": "D2"})
    )
    respx.post(url__regex=r".*/messages/D2/attachments$").mock(return_value=httpx.Response(201, json={}))
    sent = respx.post(url__regex=r".*/messages/D2/send$").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        transport = GraphTransport(http, settings, OAuthTokenProvider(http, settings))
        await transport.send(sample_message())
    assert send_mime.call_count == 1 and draft.call_count == 1 and sent.call_count == 1


async def test_graph_non_413_errors_still_raise(tmp_path):
    settings = graph_settings(tmp_path)
    with respx.mock:
        respx.post("https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token").mock(
            return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        )
        respx.post("https://graph.microsoft.com/v1.0/users/sender@example.com/sendMail").mock(
            return_value=httpx.Response(500)
        )
        async with httpx.AsyncClient() as http:
            transport = GraphTransport(http, settings, OAuthTokenProvider(http, settings))
            with pytest.raises(SendError) as exc:
                await transport.send(sample_message())
            assert exc.value.status_code == 500 and exc.value.retriable


# ----------------------------------------------------------------------------- settings & logging
def test_placeholder_and_short_secrets_are_rejected(tmp_path):
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(tmp_path, wa_app_secret="CHANGE_ME")
    with pytest.raises(ValidationError, match="at least 16"):
        make_settings(tmp_path, wa_verify_token="short")
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(tmp_path, crm_password="<your password>")
    with pytest.raises(ValidationError, match="150,000,000"):
        make_settings(tmp_path, mail_max_message_bytes=200_000_000)


def test_log_level_from_settings_wins_over_earlier_configuration():
    configure_logging("WARNING")
    configure_logging("INFO")
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("app.worker").isEnabledFor(logging.INFO)
    configure_logging("WARNING")


def test_healthz_reports_dead_worker(tmp_path):
    settings = make_settings(tmp_path, crm_enabled=False, worker_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        client.portal.call(app.state.worker_task.cancel)
        for _ in range(50):
            if app.state.worker_task.done():
                break
            client.portal.call(asyncio.sleep, 0.02)
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False and resp.json()["worker"]["alive"] is False


# ----------------------------------------------------------------------------- directory
async def test_concurrent_refreshes_download_the_list_once(clock):
    class SlowClient(FakeClient):
        async def get_list(self, entity):
            await asyncio.sleep(0.01)  # let the other callers queue up on the lock
            return await super().get_list(entity)

    client = SlowClient({"Customer": CUSTOMERS})
    directory = make_directory(client, clock)
    results = await asyncio.gather(directory.refresh(), directory.refresh(), directory.lookup("16505551234"))
    assert client.calls == ["Customer"]
    assert results[2].matched
