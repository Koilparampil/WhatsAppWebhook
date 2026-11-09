import base64
import json

import httpx
import pytest
import respx
from app.crm.client import CrmAuthError, CrmError, VShipClient, find_jwt, jwt_expiry, unwrap_envelope

BASE = "https://crm.example.test"


def make_jwt(exp: int = 4_000_000_000) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'sub': 'svc', 'exp': exp})}.{'x' * 43}"


def test_find_jwt_variants():
    token = make_jwt()
    assert find_jwt({"token": token}) == (token, "$.token")
    assert find_jwt({"data": {"accessToken": token}}) == (token, "$.data.accessToken")
    assert find_jwt(token) == (token, "$")
    assert find_jwt(f'"Bearer {token}"') == (token, "$")
    assert find_jwt({"user": {"name": "x"}, "value": {"jwt": token}}) == (token, "$.value.jwt")
    assert find_jwt({"nothing": "here"}) is None
    assert jwt_expiry(token) == 4_000_000_000.0
    assert jwt_expiry("not.a.jwt") is None


def test_unwrap_envelope():
    assert unwrap_envelope("Customer", {"isSuccess": True, "value": [{"id": 1}, "junk"]}) == [{"id": 1}]
    assert unwrap_envelope("Customer", [{"id": 2}]) == [{"id": 2}]
    assert unwrap_envelope("Customer", {"isSuccess": True, "value": None}) == []
    with pytest.raises(CrmError):
        unwrap_envelope(
            "Customer", {"isSuccess": False, "isFailure": True, "error": {"code": "X", "name": "bad"}}
        )
    with pytest.raises(CrmError):
        unwrap_envelope("Customer", "???")


@respx.mock
async def test_login_and_relogin_on_401():
    token1, token2 = make_jwt(4_000_000_000), make_jwt(4_000_000_001)
    login_route = respx.post(f"{BASE}/api/Auth/login").mock(
        side_effect=[httpx.Response(200, json={"token": token1}), httpx.Response(200, json={"token": token2})]
    )
    list_route = respx.get(f"{BASE}/api/Customer").mock(
        side_effect=[
            httpx.Response(401, json={"title": "Unauthorized"}),
            httpx.Response(
                200, json={"isSuccess": True, "isFailure": False, "error": None, "value": [{"id": 7}]}
            ),
        ]
    )
    async with httpx.AsyncClient() as http:
        client = VShipClient(http, base_url=BASE, username="u", password="p")
        rows = await client.get_list("Customer")
    assert rows == [{"id": 7}]
    assert login_route.call_count == 2
    assert list_route.call_count == 2
    assert list_route.calls[1].request.headers["Authorization"] == f"Bearer {token2}"
    assert client.token_source == "$.token"


@respx.mock
async def test_login_errors():
    respx.post(f"{BASE}/api/Auth/login").mock(return_value=httpx.Response(401, text="nope"))
    async with httpx.AsyncClient() as http:
        client = VShipClient(http, base_url=BASE, username="u", password="p")
        with pytest.raises(CrmAuthError):
            await client.login()
    respx.post(f"{BASE}/api/Auth/login").mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as http:
        client = VShipClient(http, base_url=BASE, username="u", password="p")
        with pytest.raises(CrmAuthError) as exc:
            await client.login()
        assert exc.value.retriable is False


@respx.mock
async def test_token_refresh_when_near_expiry(clock):
    token_old, token_new = make_jwt(int(clock()) + 3600), make_jwt(int(clock()) + 7200)
    respx.post(f"{BASE}/api/Auth/login").mock(
        side_effect=[
            httpx.Response(200, json={"token": token_old}),
            httpx.Response(200, json={"token": token_new}),
        ]
    )
    route = respx.get(f"{BASE}/api/Customer").mock(
        return_value=httpx.Response(200, json={"isSuccess": True, "value": []})
    )
    async with httpx.AsyncClient() as http:
        client = VShipClient(http, base_url=BASE, username="u", password="p", clock=clock)
        await client.get_list("Customer")
        clock.advance(3600 - 30)  # token_old now expires in 30s: refresh before using it
        await client.get_list("Customer")
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {token_old}"
    assert route.calls[1].request.headers["Authorization"] == f"Bearer {token_new}"
