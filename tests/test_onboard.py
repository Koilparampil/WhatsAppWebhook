from __future__ import annotations

import pytest
from app.main import create_app
from app.onboard import render_onboard_page
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import make_settings

KEY = "onboard-key-0123456789"


def onboard_settings(tmp_path, **overrides):
    base = {
        "crm_enabled": False,
        "wa_app_id": "111222333",
        "onboard_config_id": "1496722309034357",
        "onboard_key": KEY,
    }
    base.update(overrides)
    return make_settings(tmp_path, **base)


def test_page_served_with_matching_key(tmp_path):
    settings = onboard_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        r = client.get("/onboard", params={"key": KEY})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert '"1496722309034357"' in r.text and '"111222333"' in r.text and '"v27.0"' in r.text
    assert "whatsapp_business_app_onboarding" in r.text
    assert KEY not in r.text


@pytest.mark.parametrize("params", [{}, {"key": ""}, {"key": "wrong-key-wrong-key"}])
def test_wrong_or_missing_key_is_404(tmp_path, params):
    with TestClient(create_app(onboard_settings(tmp_path))) as client:
        r = client.get("/onboard", params=params)
    assert r.status_code == 404 and r.text == "not found"


@pytest.mark.parametrize("missing", ["wa_app_id", "onboard_config_id", "onboard_key"])
def test_disabled_unless_fully_configured(tmp_path, missing):
    settings = onboard_settings(tmp_path, **{missing: ""})
    assert not settings.onboard_enabled
    with TestClient(create_app(settings)) as client:
        assert client.get("/onboard", params={"key": KEY}).status_code == 404


def test_ids_must_be_numeric(tmp_path):
    with pytest.raises(ValidationError, match="WA_APP_ID must be the numeric id"):
        onboard_settings(tmp_path, wa_app_id="12</script><script>alert(1)")
    with pytest.raises(ValidationError, match="ONBOARD_CONFIG_ID must be the numeric id"):
        onboard_settings(tmp_path, onboard_config_id="abc")


def test_short_or_placeholder_key_rejected(tmp_path):
    with pytest.raises(ValidationError, match="ONBOARD_KEY must be at least 16"):
        onboard_settings(tmp_path, onboard_key="short")
    with pytest.raises(ValidationError, match="placeholder"):
        onboard_settings(tmp_path, onboard_key="CHANGE_ME")


def test_key_is_redacted(tmp_path):
    assert onboard_settings(tmp_path).redacted()["onboard_key"] == "<set>"


def test_render_escapes_script_breakout():
    page = render_onboard_page("1", "2", "v27.0</script><b>")
    assert "</script><b>" not in page
    assert "\\u003c/script\\u003e" in page
