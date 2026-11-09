"""Read-only diagnostics and the one-off webhook subscription call against the Graph API.

Used by the ``meta-check`` and ``meta-subscribe`` CLI commands so an administrator can confirm,
from the VM, that the System User token has the right scopes, that the business phone numbers are
visible and that the app is subscribed to the Messaging account (WABA) webhooks.
"""

from __future__ import annotations

from typing import Any

import httpx


class GraphAdminError(Exception):
    pass


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


async def debug_token(http: httpx.AsyncClient, *, base_url: str, version: str, token: str) -> dict[str, Any]:
    """Inspect the token with the same token (allowed for tokens of the same app)."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/debug_token",
        params={"input_token": token},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return _check(resp, "debug_token").get("data", {})


async def phone_numbers(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str
) -> list[dict]:
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/phone_numbers",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return list(_check(resp, "phone_numbers").get("data", []))


async def subscribed_apps(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str
) -> list[dict]:
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return list(_check(resp, "subscribed_apps").get("data", []))


async def subscribe_app(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, waba_id: str
) -> bool:
    resp = await http.post(
        f"{base_url.rstrip('/')}/{version}/{waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return bool(_check(resp, "subscribe").get("success"))


async def webhook_configuration(
    http: httpx.AsyncClient, *, base_url: str, version: str, token: str, phone_number_id: str
) -> dict[str, Any]:
    """Phone-number level webhook override (should normally be absent)."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/{version}/{phone_number_id}",
        params={"fields": "webhook_configuration,display_phone_number,verified_name,quality_rating"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return _check(resp, "webhook_configuration")
