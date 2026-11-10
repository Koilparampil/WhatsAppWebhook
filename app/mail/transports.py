"""Mail transports: SMTP (password, XOAUTH2 or unauthenticated relay) and Microsoft Graph.

Both expose ``async send(message) -> str`` and raise :class:`SendError` with a ``retriable`` flag
so the worker can decide between backoff and dead-lettering.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
OUTLOOK_SMTP_SCOPE = "https://outlook.office365.com/.default"
GRAPH_INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024  # larger attachments need an upload session
# sendMail takes the MIME message base64-encoded in the request body; Graph rejects bodies around
# 4 MB with HTTP 413. Compare the ENCODED size and leave headroom.
GRAPH_MIME_SEND_LIMIT = 3_500_000
GRAPH_UPLOAD_CHUNK = 10 * 327_680  # 3.125 MiB, a multiple of 320 KiB as the upload session requires


class SendError(Exception):
    def __init__(self, message: str, *, retriable: bool = True, status_code: int | None = None):
        super().__init__(message)
        self.retriable = retriable
        self.status_code = status_code


class Transport(Protocol):
    name: str

    async def send(self, message: EmailMessage) -> str: ...


def recipients_of(message: EmailMessage) -> list[str]:
    addrs: list[str] = []
    for header in ("To", "Cc", "Bcc"):
        for _, addr in getaddresses(message.get_all(header, [])):
            if addr and addr not in addrs:
                addrs.append(addr)
    return addrs


# ----------------------------------------------------------------------------- OAuth (client credentials)
class OAuthTokenProvider:
    """Caches Microsoft Entra client-credentials tokens per scope."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, clock=time.time):
        self._http = http
        self._settings = settings
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, scope: str) -> str:
        async with self._lock:
            cached = self._tokens.get(scope)
            if cached and cached[1] - 60 > self._clock():
                return cached[0]
            data = {
                "grant_type": "client_credentials",
                "client_id": self._settings.oauth_client_id,
                "client_secret": self._settings.oauth_client_secret,
                "scope": scope,
            }
            try:
                resp = await self._http.post(self._settings.oauth_token_url, data=data, timeout=30)
            except httpx.HTTPError as exc:
                raise SendError(f"OAuth token request failed: {exc!r}") from exc
            if resp.status_code != 200:
                raise SendError(
                    f"OAuth token request returned HTTP {resp.status_code}: {resp.text[:300]}",
                    retriable=resp.status_code >= 500
                    or resp.status_code == 429
                    or resp.status_code in (400, 401),
                )
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise SendError("OAuth token response had no access_token", retriable=False)
            expires_in = float(body.get("expires_in", 3600))
            self._tokens[scope] = (token, self._clock() + expires_in)
            return token


# ----------------------------------------------------------------------------- SMTP
class SmtpTransport:
    name = "smtp"

    def __init__(self, settings: Settings, oauth: OAuthTokenProvider | None = None):
        self._s = settings
        self._oauth = oauth

    async def send(self, message: EmailMessage) -> str:
        token = None
        if self._s.smtp_auth == "oauth":
            if self._oauth is None:
                raise SendError("SMTP_AUTH=oauth requires OAuth settings", retriable=False)
            token = await self._oauth.get_token(OUTLOOK_SMTP_SCOPE)
        return await asyncio.to_thread(self._send_sync, message, token)

    def _send_sync(self, message: EmailMessage, token: str | None) -> str:
        s = self._s
        context = ssl.create_default_context()
        recipients = recipients_of(message)
        if not recipients:
            raise SendError("message has no recipients", retriable=False)
        try:
            if s.smtp_security == "ssl":
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                    s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=context
                )
            else:
                smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
            with smtp:
                smtp.ehlo()
                if s.smtp_security == "starttls":
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if s.smtp_auth == "password":
                    smtp.login(s.smtp_username, s.smtp_password)
                elif s.smtp_auth == "oauth":
                    auth_string = f"user={s.smtp_username}\x01auth=Bearer {token}\x01\x01"
                    smtp.auth("XOAUTH2", lambda _challenge=None: auth_string, initial_response_ok=True)
                refused = smtp.send_message(message, from_addr=s.mail_from, to_addrs=recipients)
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(f"SMTP authentication failed: {exc}", retriable=True) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendError(
                f"SMTP refused every recipient: {exc.recipients}", retriable=_any_transient(exc.recipients)
            ) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(
                f"SMTP refused sender: {exc.smtp_code} {exc.smtp_error!r}", retriable=exc.smtp_code < 500
            ) from exc
        except smtplib.SMTPDataError as exc:
            raise SendError(
                f"SMTP rejected message data: {exc.smtp_code} {exc.smtp_error!r}",
                retriable=exc.smtp_code < 500,
            ) from exc
        except smtplib.SMTPResponseException as exc:
            raise SendError(
                f"SMTP error: {exc.smtp_code} {exc.smtp_error!r}", retriable=exc.smtp_code < 500
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise SendError(f"SMTP connection problem: {exc!r}", retriable=True) from exc
        if refused:
            log.warning("SMTP server refused some recipients: %s", refused)
        return message["Message-ID"] or ""


def _any_transient(refused: dict) -> bool:
    return any(code < 500 for code, _ in refused.values())


# ----------------------------------------------------------------------------- Microsoft Graph
class GraphTransport:
    """Sends through ``POST /users/{sender}/sendMail`` (MIME) or a draft + upload sessions for big mail."""

    name = "graph"

    def __init__(self, http: httpx.AsyncClient, settings: Settings, oauth: OAuthTokenProvider):
        self._http = http
        self._s = settings
        self._oauth = oauth

    async def _headers(self) -> dict[str, str]:
        token = await self._oauth.get_token(GRAPH_SCOPE)
        return {"Authorization": f"Bearer {token}"}

    def _url(self, path: str) -> str:
        return f"{self._s.graph_base_url.rstrip('/')}/users/{self._s.graph_sender}{path}"

    async def send(self, message: EmailMessage) -> str:
        raw = await asyncio.to_thread(message.as_bytes)
        encoded_size = 4 * ((len(raw) + 2) // 3)
        if encoded_size <= GRAPH_MIME_SEND_LIMIT:
            try:
                return await self._send_mime(raw, message)
            except SendError as exc:
                if exc.status_code != 413:
                    raise
                log.warning("Graph rejected the MIME message as too large; falling back to a draft")
        del raw
        return await self._send_as_draft(message)

    async def _send_mime(self, raw: bytes, message: EmailMessage) -> str:
        headers = await self._headers()
        headers["Content-Type"] = "text/plain"
        body = await asyncio.to_thread(base64.b64encode, raw)
        resp = await self._request("POST", self._url("/sendMail"), headers=headers, content=body)
        if resp.status_code not in (202, 200):
            self._raise_for(resp, "sendMail")
        return message["Message-ID"] or ""

    async def _send_as_draft(self, message: EmailMessage) -> str:
        headers = await self._headers()
        body_part = message.get_body(("html", "plain"))
        content_type = (
            "html" if body_part is not None and body_part.get_content_subtype() == "html" else "text"
        )
        content = body_part.get_content() if body_part is not None else ""
        reply_to = [
            {"emailAddress": {"address": a}} for _, a in getaddresses(message.get_all("Reply-To", [])) if a
        ]
        to = [{"emailAddress": {"address": a}} for _, a in getaddresses(message.get_all("To", [])) if a]
        custom_headers = [
            {"name": k, "value": str(v)}
            for k, v in message.items()
            if k.lower().startswith("x-") and len(str(v)) < 1000
        ]
        payload = {
            "subject": str(message["Subject"] or ""),
            "body": {"contentType": content_type, "content": content},
            "toRecipients": to,
            "replyTo": reply_to,
            "internetMessageHeaders": custom_headers,
        }
        resp = await self._request(
            "POST",
            self._url("/messages"),
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code not in (200, 201):
            self._raise_for(resp, "create draft")
        draft_id = resp.json().get("id")
        if not draft_id:
            raise SendError("Graph draft creation returned no id", retriable=False)

        for part in message.iter_attachments():
            data = await asyncio.to_thread(part.get_payload, decode=True) or b""
            name = part.get_filename() or "attachment.bin"
            ctype = part.get_content_type()
            if len(data) <= GRAPH_INLINE_ATTACHMENT_LIMIT:
                att = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": ctype,
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                }
                r = await self._request(
                    "POST",
                    self._url(f"/messages/{draft_id}/attachments"),
                    headers={**headers, "Content-Type": "application/json"},
                    json=att,
                )
                if r.status_code not in (200, 201):
                    self._raise_for(r, "add attachment")
            else:
                await self._upload_large(draft_id, name, data, headers)

        r = await self._request(
            "POST", self._url(f"/messages/{draft_id}/send"), headers={**headers, "Content-Length": "0"}
        )
        if r.status_code not in (202, 200):
            self._raise_for(r, "send draft")
        return message["Message-ID"] or draft_id

    async def _upload_large(self, draft_id: str, name: str, data: bytes, headers: dict[str, str]) -> None:
        session_payload = {"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(data)}}
        r = await self._request(
            "POST",
            self._url(f"/messages/{draft_id}/attachments/createUploadSession"),
            headers={**headers, "Content-Type": "application/json"},
            json=session_payload,
        )
        if r.status_code not in (200, 201):
            self._raise_for(r, "create upload session")
        upload_url = r.json().get("uploadUrl")
        if not upload_url:
            raise SendError("Graph upload session returned no uploadUrl", retriable=False)
        total = len(data)
        offset = 0
        while offset < total:
            chunk = data[offset : offset + GRAPH_UPLOAD_CHUNK]
            end = offset + len(chunk) - 1
            put_headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            }
            r = await self._request("PUT", upload_url, headers=put_headers, content=chunk)
            if r.status_code not in (200, 201, 202):
                self._raise_for(r, "upload chunk")
            offset += len(chunk)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, url, timeout=120, **kwargs)
        except httpx.HTTPError as exc:
            raise SendError(f"Graph request failed: {exc!r}") from exc

    @staticmethod
    def _raise_for(resp: httpx.Response, step: str) -> None:
        # 404 is included: a draft created a moment ago can briefly be missing on another replica
        retriable = resp.status_code >= 500 or resp.status_code in (401, 403, 404, 408, 429)
        raise SendError(
            f"Graph {step} returned HTTP {resp.status_code}: {resp.text[:300]}",
            retriable=retriable,
            status_code=resp.status_code,
        )


def build_transport(settings: Settings, http: httpx.AsyncClient) -> Transport:
    oauth = (
        OAuthTokenProvider(http, settings)
        if (settings.oauth_client_id and settings.oauth_client_secret)
        else None
    )
    if settings.mail_transport == "graph":
        if oauth is None:
            raise ValueError("MAIL_TRANSPORT=graph needs OAUTH_* settings")
        return GraphTransport(http, settings, oauth)
    return SmtpTransport(settings, oauth)
