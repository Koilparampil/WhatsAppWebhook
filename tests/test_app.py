from __future__ import annotations

import base64
import hashlib
import json
import time
from email import message_from_bytes
from email.policy import SMTP

import httpx
import respx
from app.main import create_app
from app.security import compute_signature
from fastapi.testclient import TestClient

from tests.conftest import load_payload, make_settings, text_payload
from tests.test_crm_client import make_jwt
from tests.test_transports import start_sink


def signed_post(client: TestClient, settings, payload: dict, secret: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": compute_signature(secret or settings.wa_app_secret, body),
    }
    return client.post("/webhook", content=body, headers=headers)


def test_verification_handshake(tmp_path):
    settings = make_settings(tmp_path, crm_enabled=False)
    with TestClient(create_app(settings)) as client:
        ok = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"},
        )
        assert ok.status_code == 200 and ok.text == "1158201444"
        bad = client.get(
            "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}
        )
        assert bad.status_code == 403
        assert client.get("/webhook").status_code == 403


def test_post_requires_signature_and_dedupes(tmp_path):
    settings = make_settings(tmp_path, crm_enabled=False)
    with TestClient(create_app(settings)) as client:
        payload = text_payload()
        unsigned = client.post("/webhook", json=payload)
        assert unsigned.status_code == 403
        wrong = signed_post(client, settings, payload, secret="other")
        assert wrong.status_code == 403
        first = signed_post(client, settings, payload)
        assert first.status_code == 200 and first.json() == {"status": "ok", "queued": 1, "duplicates": 0}
        again = signed_post(client, settings, payload)
        assert again.json() == {"status": "ok", "queued": 0, "duplicates": 1}
        statuses = signed_post(client, settings, load_payload("statuses"))
        assert statuses.json()["queued"] == 0
        bad_json = client.post(
            "/webhook",
            content=b"{not json",
            headers={"X-Hub-Signature-256": compute_signature(settings.wa_app_secret, b"{not json")},
        )
        assert bad_json.status_code == 400
        health = client.get("/healthz").json()
        assert health["ok"] is True and health["queue"]["pending"] == 1
        assert health["crm"]["enabled"] is False


def test_unsigned_allowed_in_dev_mode(tmp_path):
    settings = make_settings(tmp_path, crm_enabled=False, wa_allow_unsigned=True)
    with TestClient(create_app(settings)) as client:
        assert client.post("/webhook", json=text_payload()).status_code == 200


@respx.mock
def test_end_to_end_image_with_crm_match(tmp_path):
    handler, controller, port = start_sink()
    try:
        settings = make_settings(tmp_path, smtp_port=port, worker_enabled=True, crm_refresh_seconds=3600)
        respx.post("https://crm.example.test/api/Auth/login").mock(
            return_value=httpx.Response(200, json={"token": make_jwt()})
        )
        respx.get("https://crm.example.test/api/Customer").mock(
            return_value=httpx.Response(
                200,
                json={
                    "isSuccess": True,
                    "isFailure": False,
                    "error": None,
                    "value": [
                        {
                            "id": 42,
                            "companyName": "Acme Imports",
                            "contactPerson": "John Doe",
                            "mobile": "(650) 555-1234",
                            "email1": "john@acme.com",
                            "email2": "ops@acme.com",
                            "active": True,
                        }
                    ],
                },
            )
        )
        data = b"\xff\xd8" + b"p" * 4000
        digest = hashlib.sha256(data).digest()
        respx.get("https://graph.facebook.com/v26.0/1385223789055226").mock(
            return_value=httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/img",
                    "mime_type": "image/jpeg",
                    "file_size": len(data),
                },
            )
        )
        respx.get("https://lookaside.fbsbx.com/img").mock(return_value=httpx.Response(200, content=data))
        payload = load_payload("image")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["sha256"] = base64.b64encode(
            digest
        ).decode()

        with TestClient(create_app(settings)) as client:
            resp = signed_post(client, settings, payload)
            assert resp.json()["queued"] == 1
            deadline = time.time() + 15
            while time.time() < deadline and not handler.envelopes:
                time.sleep(0.1)
            assert handler.envelopes, "email was not delivered within 15s"
            health = client.get("/healthz").json()
            assert health["queue"]["done"] == 1
            assert health["crm"]["indexed_numbers"] == 1
        env = handler.envelopes[0]
        delivered = message_from_bytes(env.content, policy=SMTP)
        assert delivered["Reply-To"] == "john@acme.com, ops@acme.com"
        assert (
            delivered["Subject"]
            == "[WhatsApp] Sheena Nelson (+1 650-555-1234): sent an image: Here is the damaged box"
        )
        assert delivered["X-WhatsApp-CRM-Match"] == "customer:42"
        attachment = next(delivered.iter_attachments())
        assert attachment.get_content_type() == "image/jpeg"
        assert attachment.get_payload(decode=True) == data
        assert set(env.rcpt_tos) == {"ops@example.com", "second@example.com"}
    finally:
        controller.stop()
