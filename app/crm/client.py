"""Minimal VShipCRM API client: login, bearer-token refresh, and list endpoints.

The OpenAPI document does not describe the login response body, so the JWT is located by
inspection (well-known keys first, then any JWT-shaped string). ``token_source`` records where it
was found so the CLI can report it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
_PREFERRED_TOKEN_KEYS = (
    "token",
    "accessToken",
    "access_token",
    "jwt",
    "jwtToken",
    "bearerToken",
    "idToken",
    "id_token",
)


class CrmError(Exception):
    def __init__(self, message: str, *, retriable: bool = True, status_code: int | None = None):
        super().__init__(message)
        self.retriable = retriable
        self.status_code = status_code


class CrmAuthError(CrmError):
    pass


def find_jwt(payload: Any, path: str = "$") -> tuple[str, str] | None:
    """Return (token, json-path) for the first JWT found in a decoded response, preferring known keys."""
    if isinstance(payload, str):
        text = payload.strip().strip('"')
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        return (text, path) if _JWT_RE.match(text) else None
    if isinstance(payload, dict):
        for key in _PREFERRED_TOKEN_KEYS:
            for actual in payload:
                if actual.lower() == key.lower():
                    found = find_jwt(payload[actual], f"{path}.{actual}")
                    if found:
                        return found
        for key, value in payload.items():
            found = find_jwt(value, f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            found = find_jwt(value, f"{path}[{index}]")
            if found:
                return found
    return None


def jwt_expiry(token: str) -> float | None:
    """Read the ``exp`` claim without verifying the signature (we only use it to refresh early)."""
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, binascii.Error, AttributeError, TypeError):
        return None


class VShipClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 20.0,
        clock=time.time,
    ):
        self._http = http
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._token_exp: float | None = None
        self.token_source: str | None = None
        self.last_login_at: float | None = None

    # ------------------------------------------------------------------ auth
    async def login(self) -> None:
        url = f"{self.base_url}/api/Auth/login"
        try:
            resp = await self._http.post(
                url,
                json={"username": self._username, "password": self._password},
                headers={"Accept": "application/json, text/plain"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CrmError(f"CRM login request failed: {exc!r}") from exc
        if resp.status_code in (400, 401, 403):
            raise CrmAuthError(
                f"CRM login rejected (HTTP {resp.status_code}): {resp.text[:200]}",
                retriable=True,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise CrmError(
                f"CRM login returned HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code
            )
        payload: Any
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        found = find_jwt(payload)
        if not found:
            raise CrmAuthError("CRM login succeeded but no JWT was found in the response", retriable=False)
        self._token, self.token_source = found
        self._token_exp = jwt_expiry(self._token)
        self.last_login_at = self._clock()
        log.info("CRM login ok (token at %s, expires %s)", self.token_source, self._token_exp)

    def _token_is_fresh(self) -> bool:
        if not self._token:
            return False
        if self._token_exp is None:
            return True
        return self._token_exp - 60 > self._clock()

    async def ensure_token(self) -> str:
        if not self._token_is_fresh():
            await self.login()
        assert self._token is not None
        return self._token

    def forget_token(self) -> None:
        self._token = None
        self._token_exp = None

    # ------------------------------------------------------------------ data
    async def get_list(self, entity: str) -> list[dict[str, Any]]:
        """GET /api/{entity} and unwrap the ``{isSuccess, error, value}`` envelope."""
        url = f"{self.base_url}/api/{entity}"
        for attempt in (1, 2):
            token = await self.ensure_token()
            try:
                resp = await self._http.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/plain"},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                raise CrmError(f"CRM request {entity} failed: {exc!r}") from exc
            if resp.status_code == 401 and attempt == 1:
                log.info("CRM returned 401 for %s; re-authenticating", entity)
                self.forget_token()
                continue
            if resp.status_code == 401:
                raise CrmAuthError(f"CRM rejected credentials for {entity}", status_code=401)
            if resp.status_code != 200:
                raise CrmError(
                    f"CRM {entity} returned HTTP {resp.status_code}: {resp.text[:200]}",
                    retriable=resp.status_code >= 500 or resp.status_code == 429,
                    status_code=resp.status_code,
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise CrmError(f"CRM {entity} response was not JSON") from exc
            return unwrap_envelope(entity, payload)
        raise CrmError(f"CRM {entity}: unexpected retry exhaustion")  # pragma: no cover


def unwrap_envelope(entity: str, payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if "isSuccess" in payload or "isFailure" in payload:
            if payload.get("isFailure") or payload.get("isSuccess") is False:
                err = payload.get("error") or {}
                raise CrmError(
                    f"CRM {entity} reported failure: {err.get('code')} {err.get('name')}".strip(),
                    retriable=False,
                )
            value = payload.get("value")
            if value is None:
                return []
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            raise CrmError(f"CRM {entity} value was not a list", retriable=False)
        if isinstance(payload.get("value"), list):
            return [item for item in payload["value"] if isinstance(item, dict)]
    raise CrmError(f"CRM {entity} returned an unexpected response shape", retriable=False)
