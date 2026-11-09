import httpx
import pytest
import respx
from app.whatsapp import admin

GRAPH = "https://graph.facebook.com"
COMMON = {"base_url": GRAPH, "version": "v27.0", "token": "tok"}


@respx.mock
async def test_debug_token_and_waba_calls():
    respx.get(f"{GRAPH}/v27.0/debug_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "app_id": "1",
                    "type": "SYSTEM_USER",
                    "is_valid": True,
                    "expires_at": 0,
                    "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
                }
            },
        )
    )
    respx.get(f"{GRAPH}/v27.0/WABA1/phone_numbers").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "PN1", "display_phone_number": "+1 555-078-3881"}]}
        )
    )
    respx.get(f"{GRAPH}/v27.0/WABA1/subscribed_apps").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    sub = respx.post(f"{GRAPH}/v27.0/WABA1/subscribed_apps").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    async with httpx.AsyncClient() as http:
        info = await admin.debug_token(http, **COMMON)
        assert info["type"] == "SYSTEM_USER" and "whatsapp_business_messaging" in info["scopes"]
        numbers = await admin.phone_numbers(http, waba_id="WABA1", **COMMON)
        assert numbers[0]["id"] == "PN1"
        assert await admin.subscribed_apps(http, waba_id="WABA1", **COMMON) == []
        assert await admin.subscribe_app(http, waba_id="WABA1", **COMMON) is True
    assert sub.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_graph_admin_error_is_descriptive():
    respx.get(f"{GRAPH}/v27.0/WABA1/phone_numbers").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Unsupported get request", "code": 100, "error_subcode": 33}}
        )
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(admin.GraphAdminError) as exc:
            await admin.phone_numbers(http, waba_id="WABA1", **COMMON)
    assert "Unsupported get request" in str(exc.value) and "code 100" in str(exc.value)
