"""Read-only diagnostics and the one-off webhook subscription call against the Graph API.

Used by the ``meta-check`` and ``meta-subscribe`` CLI commands so an administrator can confirm,
from the VM, that the System User token has the right scopes, that the business phone numbers are
visible and that the app is subscribed to the Messaging account (WABA) webhooks.

Every call sends ``appsecret_proof`` when the App Secret is known, so the app can run with
"Require App Secret" enabled in the App Dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx


class GraphAdminError(Exception):
    pass


def appsecret_proof(access_token: str, app_secret: str) -> str:
    """HMAC-SHA256 of the access token keyed with the App Secret (Meta's ``appsecret_proof``)."""
    return hmac.new(app_secret.encode("utf-8"), access_token.encode("utf-8"), hashlib.sha256).hexdigest()


def graph_params(access_token: str, app_secret: str, **extra: str) -> dict[str, str]:
    params = dict(extra)
    if app_secret:
        params["appsecret_proof"] = appsecret_proof(access_token, app_secret)
    return params


def _check(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    if resp.status_code != 200:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise GraphAdminError(
            f"{what} failed: HTTP {resp.status_code} {err.get('message') or body}"
            + (f" (code {err.get('code')}, subcode {err.get('error_subcode')})" if err else "")
        )
    return body if isinstance(body, dict) else {"data": body}


async def debug_token(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, app_secret: str = ""
) -> dict[str, Any]:
    """Inspect the token with the same token (allowed for tokens of the same app)."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/debug_token",
        params=graph_params(token, app_secret, input_token=token),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return _check(resp, "debug_token").get("data", {})


async def phone_numbers(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str, app_secret: str = ""
) -> list[dict]:
    """Numbers of the Messaging account. The list carries id, display number, name and quality only."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/phone_numbers",
        params=graph_params(token, app_secret),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return list(_check(resp, "phone_numbers").get("data", []))


async def phone_number_details(
    http: httpx.AsyncClient,
    *,
    base_url: str,
    version: str,
    token: str,
    phone_number_id: str,
    app_secret: str = "",
    fields: str = (
        "display_phone_number,verified_name,quality_rating,status,code_verification_status,"
        "name_status,webhook_configuration"
    ),
) -> dict[str, Any]:
    """Single-number fields, including the connection status and any phone-level webhook override."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{phone_number_id}",
        params=graph_params(token, app_secret, fields=fields),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return _check(resp, "phone_number")


async def subscribed_apps(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str, app_secret: str = ""
) -> list[dict]:
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/subscribed_apps",
        params=graph_params(token, app_secret),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return list(_check(resp, "subscribed_apps").get("data", []))


async def subscribe_app(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str, app_secret: str = ""
) -> bool:
    resp = await http.post(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/subscribed_apps",
        params=graph_params(token, app_secret),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return bool(_check(resp, "subscribe").get("success"))


# kept for callers that only need the override information
async def webhook_configuration(
    http: httpx.AsyncClient,
    *,
    base_url: str,
    version: str,
    token: str,
    phone_number_id: str,
    app_secret: str = "",
) -> dict[str, Any]:
    return await phone_number_details(
        http,
        base_url=base_url,
        version=version,
        token=token,
        phone_number_id=phone_number_id,
        app_secret=app_secret,
    )
