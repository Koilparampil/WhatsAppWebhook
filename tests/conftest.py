from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "wa_verify_token": "verify-me-verify-me",
        "wa_app_secret": "app-secret-app-secret",
        "wa_access_token": "wa-token",
        "crm_enabled": True,
        "crm_username": "svc",
        "crm_password": "pw",
        "crm_base_url": "https://crm.example.test",
        "mail_transport": "smtp",
        "smtp_host": "127.0.0.1",
        "smtp_port": 2525,
        "smtp_security": "none",
        "smtp_auth": "none",
        "mail_from": "whatsapp@example.com",
        "mail_to": "ops@example.com, second@example.com",
        "mail_timezone": "America/New_York",
        "store_path": tmp_path / "queue.db",
        "spool_dir": tmp_path / "spool",
        "worker_enabled": False,
        "worker_poll_seconds": 0.05,
        "worker_base_delay_seconds": 15,
        "worker_max_delay_seconds": 120,
        "worker_retry_window_seconds": 600,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def load_payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def text_payload(
    body: str = "Does it come in another color?",
    *,
    wamid: str = "wamid.HBgLMTY1MDU1NTEyMzQVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=",
    from_phone: str | None = "16505551234",
    name: str | None = "Sheena Nelson",
    username: str | None = None,
    bsuid: str | None = "US.13491208655302741918",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = copy.deepcopy(load_payload("text"))
    value = payload["entry"][0]["changes"][0]["value"]
    message = value["messages"][0]
    contact = value["contacts"][0]
    message["id"] = wamid
    message["text"]["body"] = body
    if from_phone is None:
        message.pop("from", None)
        contact.pop("wa_id", None)
    else:
        message["from"] = from_phone
        contact["wa_id"] = from_phone
    if name is None:
        contact["profile"].pop("name", None)
    else:
        contact["profile"]["name"] = name
    if username:
        contact["profile"]["username"] = username
    if bsuid is None:
        message.pop("from_user_id", None)
        contact.pop("user_id", None)
    else:
        message["from_user_id"] = bsuid
        contact["user_id"] = bsuid
    if context:
        message["context"] = context
    return payload


def media_payload(kind: str, **fields: Any) -> dict[str, Any]:
    payload = copy.deepcopy(load_payload("image"))
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    media = message.pop("image")
    media.update(fields)
    message["type"] = kind
    message[kind] = media
    message["id"] = f"wamid.{kind.upper()}123"
    return payload
