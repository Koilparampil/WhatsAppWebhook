"""Regression tests for the third review round (spec, deploy and external API findings)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from app.crm.client import CrmError
from app.crm.directory import LookupStatus
from app.mail.compose import build_emails, build_test_email
from app.mail.transports import (
    GraphTransport,
    OAuthTokenProvider,
    SendError,
    graph_custom_headers,
    parse_retry_after,
)
from app.store import KIND_MESSAGE, Store
from app.whatsapp.media import DownloadedMedia
from app.whatsapp.parser import parse_webhook
from app.worker import Worker

from tests.conftest import load_payload, make_settings, text_payload
from tests.test_compose import matched_lookup
from tests.test_directory import CUSTOMERS, FakeClient, make_directory
from tests.test_transports import graph_settings, sample_message
from tests.test_worker import FakeDirectory

GRAPH = "https://graph.microsoft.com/v1.0/users/sender@example.com"
TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"


# ----------------------------------------------------------------------------- Graph header cap
def test_split_part_from_bsuid_sender_sends_at_most_five_custom_headers(tmp_path):
    settings = make_settings(tmp_path, mail_max_message_bytes=1_000_000)
    path = tmp_path / "doc"
    path.write_bytes(os.urandom(1_500_000))
    media = DownloadedMedia(
        path=path,
        size=1_500_000,
        mime_type="application/pdf",
        filename="doc.pdf",
        sha256_hex="0" * 64,
        integrity_verified=True,
    )
    msg = parse_webhook(load_payload("image")).messages[0]
    assert msg.from_bsuid  # the fixture sender carries a business-scoped user id
    part = build_emails(msg, matched_lookup(), media, settings)[0].message
    assert len([k for k in part.keys() if k.lower().startswith("x-")]) == 6  # the MIME email keeps all
    headers = graph_custom_headers(part)
    assert len(headers) == 5
    assert [h["name"] for h in headers][:3] == [
        "X-WhatsApp-Message-Id",
        "X-WhatsApp-From",
        "X-WhatsApp-CRM-Match",
    ]


@respx.mock
async def test_graph_draft_is_retried_without_headers_when_graph_refuses_them(tmp_path, monkeypatch):
    from app.mail import transports

    monkeypatch.setattr(transports, "GRAPH_MIME_SEND_LIMIT", 10)
    settings = graph_settings(tmp_path)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    drafts = respx.post(f"{GRAPH}/messages").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"code": "InvalidInternetMessageHeaderCollection"}}),
            httpx.Response(201, json={"id": "D9"}),
        ]
    )
    respx.post(f"{GRAPH}/messages/D9/attachments").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{GRAPH}/messages/D9/send").mock(return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        await GraphTransport(http, settings, OAuthTokenProvider(http, settings)).send(sample_message())
    import json as _json

    assert "internetMessageHeaders" in _json.loads(drafts.calls[0].request.content)
    assert "internetMessageHeaders" not in _json.loads(drafts.calls[1].request.content)


# ----------------------------------------------------------------------------- Retry-After
def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None and parse_retry_after("soon") is None
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    later = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(later, now=now.timestamp()) == pytest.approx(90, abs=1)
    assert parse_retry_after("999999") == 3600.0  # capped


@respx.mock
async def test_graph_429_carries_retry_after_and_worker_pauses_all_sends(tmp_path, clock):
    settings = graph_settings(tmp_path)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )
    send = respx.post(f"{GRAPH}/sendMail").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    async with httpx.AsyncClient() as http:
        transport = GraphTransport(http, settings, OAuthTokenProvider(http, settings))
        with pytest.raises(SendError) as exc:
            await transport.send(sample_message())
        assert exc.value.retry_after == 120.0 and exc.value.retriable

        store = Store(settings.store_path)
        worker = Worker(settings, store, FakeDirectory(matched_lookup()), transport, http, clock=clock)
        for key in ("wamid.T1", "wamid.T2"):
            store.enqueue(KIND_MESSAGE, key, {"envelope": text_payload(wamid=key)}, clock())
        await worker.handle(store.claim_next(clock()))
    first = store.get("wamid.T1")
    assert first.next_attempt_at - clock() >= 120  # waited as long as Graph asked, not 15 s
    assert worker.paused_until == pytest.approx(clock() + 120)
    assert send.call_count == 2  # the direct call above plus one worker attempt; T2 was not tried


# ----------------------------------------------------------------------------- stale tokens
@respx.mock
async def test_graph_401_drops_the_cached_token_and_retries_once(tmp_path):
    settings = graph_settings(tmp_path)
    tokens = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "OLD", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600}),
        ]
    )

    def by_token(request):
        return httpx.Response(202 if request.headers["Authorization"] == "Bearer NEW" else 401)

    send = respx.post(f"{GRAPH}/sendMail").mock(side_effect=by_token)
    async with httpx.AsyncClient() as http:
        await GraphTransport(http, settings, OAuthTokenProvider(http, settings)).send(sample_message())
    assert tokens.call_count == 2 and send.call_count == 2


# ----------------------------------------------------------------------------- CRM outage reporting
async def test_miss_during_crm_outage_is_reported_as_a_crm_error(clock):
    client = FakeClient({"Customer": CUSTOMERS})
    directory = make_directory(client, clock)
    await directory.refresh()
    client.fail = True
    clock.advance(180 * 60)
    result = await directory.lookup("16465550199")
    assert result.status is LookupStatus.ERROR
    assert "could not be reached" in result.reason and "180 min" in result.reason
    # a number that IS in the cached index still matches during the outage
    assert (await directory.lookup("16505551234")).status is LookupStatus.MATCHED


async def test_failing_crm_is_not_called_for_every_miss(clock):
    client = FakeClient({"Customer": CUSTOMERS})
    directory = make_directory(client, clock)
    await directory.refresh()
    client.fail = True
    clock.advance(120)
    for i in range(10):
        await directory.lookup(f"1646555{i:04d}")
    assert client.calls == ["Customer", "Customer"]  # one attempt within the 60 s gate, not ten
    clock.advance(61)
    await directory.lookup("16465559999")
    assert client.calls == ["Customer", "Customer", "Customer"]
    with pytest.raises(CrmError):
        client.fail = True
        clock.advance(61)
        await directory.refresh()


async def test_crm_down_since_startup_is_also_rate_limited(clock):
    client = FakeClient({"Customer": CUSTOMERS})
    client.fail = True
    directory = make_directory(client, clock)
    for _ in range(5):
        assert (await directory.lookup("16505551234")).status is LookupStatus.ERROR
    assert client.calls == ["Customer"]


def test_no_email_status_renders_as_customer_found(settings):
    from app.crm.directory import LookupResult

    lookup = matched_lookup()
    no_email = LookupResult(
        LookupStatus.NO_EMAIL,
        candidates=lookup.candidates,
        reason="Customer #42: Acme Imports / John Doe has no email address in the CRM",
        entities_searched=["Customer"],
    )
    msg = parse_webhook(text_payload()).messages[0]
    m = build_emails(msg, no_email, None, settings)[0].message
    body = m.get_body(("plain",)).get_content()
    assert "Customer found, but no Reply-To possible" in body
    assert m["X-WhatsApp-CRM-Match"] == "no_email" and "Reply-To" not in m


# ----------------------------------------------------------------------------- operator UX
def test_test_email_is_not_shaped_like_an_alert(settings):
    m = build_test_email(settings, "graph", "2026-11-10T15:00:00-05:00")
    assert "ALERT" not in m["Subject"] and m["X-WhatsApp-Alert"] is None and m["X-WhatsApp-Test"] == "1"
    assert "2026-11-10T15:00:00-05:00" in m.get_content()


def test_cli_times_use_the_configured_timezone():
    from app.cli import _fmt_ts

    ts = datetime(2026, 11, 10, 20, 0, tzinfo=UTC).timestamp()
    assert _fmt_ts(ts, "America/New_York") == "2026-11-10T15:00:00-05:00"
    assert _fmt_ts(None, "America/New_York") == "-"


def test_default_timezone_is_new_york():
    from app.config import Settings

    assert Settings.model_fields["mail_timezone"].default == "America/New_York"


def test_cli_exits_78_on_configuration_errors(tmp_path):
    env = tmp_path / "bad.env"
    env.write_text("WA_VERIFY_TOKEN=x\n")
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("WA_", "MAIL_", "CRM_", "SMTP_"))}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.cli", "--env-file", str(env), "check-config"],
        capture_output=True,
        text=True,
        env=clean_env,
        check=False,
    )
    assert proc.returncode == 78
    assert "configuration error" in proc.stderr


def test_skipped_messages_name_the_business_number():
    parsed = parse_webhook(load_payload("text"), phone_number_id_filter="999")
    assert parsed.skipped_messages == 1 and parsed.skipped_phone_number_ids == {"106540352242922"}


# ----------------------------------------------------------------------------- meta-check routing report
def test_meta_check_reports_phone_and_waba_overrides(capsys):
    from app.cli import _report_webhook_routing

    rc = _report_webhook_routing(
        "106540352242922",
        {
            "webhook_configuration": {
                "application": "https://whatsapphook.vship2000.com/webhook",
                "phone_number": "https://old-bsp.example.net/hook",
            }
        },
    )
    out = capsys.readouterr().out
    assert rc == 2 and "PHONE-level callback override: https://old-bsp.example.net/hook" in out
    assert "app callback=https://whatsapphook.vship2000.com/webhook" in out
    assert _report_webhook_routing("1", {"webhook_configuration": {"application": "https://x"}}) == 0


@respx.mock
async def test_meta_check_flags_waba_override_from_subscribed_apps(tmp_path, monkeypatch, capsys):
    from app import cli

    settings = make_settings(tmp_path, wa_waba_id="WABA1")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    base = "https://graph.facebook.com/v27.0"
    respx.get(f"{base}/debug_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "type": "SYSTEM_USER",
                    "is_valid": True,
                    "expires_at": 0,
                    "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"],
                }
            },
        )
    )
    respx.get(f"{base}/WABA1/phone_numbers").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "PN1"}]})
    )
    respx.get(f"{base}/PN1").mock(
        return_value=httpx.Response(
            200, json={"status": "CONNECTED", "webhook_configuration": {"application": "https://a"}}
        )
    )
    respx.get(f"{base}/WABA1/subscribed_apps").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"whatsapp_business_api_data": {"id": "1"}, "override_callback_uri": "https://old"}]
            },
        )
    )
    rc = await cli.cmd_meta_check(None)
    out = capsys.readouterr().out
    assert rc == 2 and "WABA-level callback override: https://old" in out and "status=CONNECTED" in out
