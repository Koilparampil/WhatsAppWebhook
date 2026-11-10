"""Mail transports: SMTP (password, XOAUTH2 or unauthenticated relay) and Microsoft Graph.

Both expose ``async send(message) -> str`` and raise :class:`SendError` with a ``retriable`` flag
so the worker can decide between backoff and dead-lettering.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import gc
import logging
import smtplib
import ssl
import time
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
OUTLOOK_SMTP_SCOPE = "https://outlook.office365.com/.default"
# Graph: attachments added inline must be "under 3 MB"; upload sessions take 3 MB to 150 MB.
GRAPH_INLINE_ATTACHMENT_LIMIT = 3_000_000
# sendMail takes the MIME message base64-encoded in the request body; Graph rejects bodies around
# 4 MB with HTTP 413. Compare the ENCODED size and leave headroom.
GRAPH_MIME_SEND_LIMIT = 3_500_000
GRAPH_UPLOAD_CHUNK = 10 * 327_680  # 3.125 MiB, a multiple of 320 KiB as the upload session requires


B64_DECODE_BLOCK_CHARS = 1 << 20


def b64_decoded_size(text: str) -> int:
    """Exact decoded length of a (line-wrapped) base64 string without decoding it."""
    significant = len(text) - sum(text.count(ch) for ch in "\r\n \t")
    padding = text[-8:].rstrip()[-2:].count("=")  # look only at the tail: no copy of the whole text
    return significant // 4 * 3 - padding


def iter_b64_blocks(text: str, block_chars: int = B64_DECODE_BLOCK_CHARS) -> Iterator[bytes]:
    """Decode line-wrapped base64 in blocks so the binary never exists as one large copy."""
    carry = ""
    for start in range(0, len(text), block_chars):
        piece = carry + "".join(text[start : start + block_chars].split())
        cut = len(piece) - len(piece) % 4
        carry = piece[cut:]
        if cut:
            yield binascii.a2b_base64(piece[:cut])
    if carry:
        raise SendError("attachment base64 data is truncated", retriable=False)


class SendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retriable: bool = True,
        status_code: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.retriable = retriable
        self.status_code = status_code
        self.retry_after = retry_after  # seconds the server asked us to wait (HTTP 429/503)


class SendAuthError(SendError):
    """The mail server or Graph rejected our credentials or token."""


MAX_RETRY_AFTER_SECONDS = 3600.0


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP-date (RFC 9110); None when absent/invalid."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


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
                    status_code=resp.status_code,
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                )
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise SendError("OAuth token response had no access_token", retriable=False)
            expires_in = float(body.get("expires_in", 3600))
            self._tokens[scope] = (token, self._clock() + expires_in)
            return token

    def invalidate(self, scope: str) -> None:
        """Forget a token the server rejected so the next call fetches a fresh one."""
        self._tokens.pop(scope, None)


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
        try:
            return await asyncio.to_thread(self._send_sync, message, token)
        except SendAuthError:
            if self._oauth is not None:
                self._oauth.invalidate(OUTLOOK_SMTP_SCOPE)  # the next attempt gets a fresh token
            raise

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
            raise SendAuthError(f"SMTP authentication failed: {exc}", retriable=True) from exc
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
        try:
            return await self._send_once(message)
        except SendError as exc:
            if exc.status_code != 401:
                raise
            # a cached token Graph no longer accepts (e.g. permissions changed): retry once fresh
            log.warning("Graph rejected the access token (401); retrying once with a new token")
            self._oauth.invalidate(GRAPH_SCOPE)
            return await self._send_once(message)

    async def _send_once(self, message: EmailMessage) -> str:
        # The attachments' base64 text alone tells whether the MIME route can fit; when it cannot,
        # skip flattening the whole message (a full extra copy of up to MAIL_MAX_MESSAGE_BYTES).
        attachment_text = sum(len(part.get_payload()) for part in message.iter_attachments())
        if 4 * ((attachment_text + 2) // 3) <= GRAPH_MIME_SEND_LIMIT:
            raw = await asyncio.to_thread(message.as_bytes)
            if 4 * ((len(raw) + 2) // 3) <= GRAPH_MIME_SEND_LIMIT:
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
        custom_headers = graph_custom_headers(message)
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
        if resp.status_code == 400 and "InternetMessageHeader" in resp.text:
            log.warning(
                "Graph refused the custom headers (%s); creating the draft without them", resp.text[:200]
            )
            payload.pop("internetMessageHeaders")
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

        try:
            for part in message.iter_attachments():
                name = part.get_filename() or "attachment.bin"
                ctype = part.get_content_type()
                payload = part.get_payload()
                cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
                if isinstance(payload, str) and cte == "base64":
                    # decode block by block from the message's base64 text: no full binary copy
                    size = b64_decoded_size(payload)

                    def source(text: str = payload) -> Iterator[bytes]:
                        return iter_b64_blocks(text)
                else:
                    data = part.get_payload(decode=True) or b""
                    size = len(data)

                    def source(blob: bytes = data) -> Iterator[bytes]:
                        return iter((blob,))

                await self._attach(draft_id, name, ctype, size, source, headers)
            r = await self._request(
                "POST", self._url(f"/messages/{draft_id}/send"), headers={**headers, "Content-Length": "0"}
            )
            if r.status_code not in (202, 200):
                self._raise_for(r, "send draft")
        except SendError:
            await self._delete_draft(draft_id, headers)
            raise
        return message["Message-ID"] or draft_id

    async def _attach(
        self,
        draft_id: str,
        name: str,
        ctype: str,
        size: int,
        source: Callable[[], Iterator[bytes]],
        headers: dict[str, str],
    ) -> None:
        """Inline for files under 3 MB, upload session otherwise; each falls back to the other once."""
        if size < GRAPH_INLINE_ATTACHMENT_LIMIT:
            try:
                await self._attach_inline(draft_id, name, ctype, b"".join(source()), headers)
            except SendError as exc:
                if exc.status_code != 413:
                    raise
                log.warning("Graph refused %s inline (413); using an upload session", name)
                await self._upload_stream(draft_id, name, size, source(), headers)
        else:
            try:
                await self._upload_stream(draft_id, name, size, source(), headers)
            except SendError as exc:
                if exc.status_code != 400 or "MinimumSize" not in str(exc):
                    raise
                log.warning("Graph says %s is too small for an upload session; attaching inline", name)
                await self._attach_inline(draft_id, name, ctype, b"".join(source()), headers)

    async def _attach_inline(
        self, draft_id: str, name: str, ctype: str, data: bytes, headers: dict[str, str]
    ) -> None:
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

    async def _delete_draft(self, draft_id: str, headers: dict[str, str]) -> None:
        """Best effort: do not leave half-built drafts behind in the relay mailbox."""
        try:
            await self._http.request(
                "DELETE", self._url(f"/messages/{draft_id}"), headers=headers, timeout=30
            )
        except httpx.HTTPError as exc:
            log.warning("could not delete unsent draft %s: %r", draft_id, exc)

    async def _upload_large(self, draft_id: str, name: str, data: bytes, headers: dict[str, str]) -> None:
        await self._upload_stream(draft_id, name, len(data), iter((data,)), headers)

    async def _upload_stream(
        self, draft_id: str, name: str, total: int, blocks: Iterator[bytes], headers: dict[str, str]
    ) -> None:
        """createUploadSession, then PUT sequential ranges (no Authorization header, per Graph docs)."""
        session_payload = {"AttachmentItem": {"attachmentType": "file", "name": name, "size": total}}
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
        buffer = bytearray()
        offset = 0

        async def put(chunk: bytes) -> None:
            nonlocal offset
            end = offset + len(chunk) - 1
            put_headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            }
            resp = await self._request("PUT", upload_url, headers=put_headers, content=chunk)
            if resp.status_code not in (200, 201, 202):
                self._raise_for(resp, "upload chunk")
            offset += len(chunk)

        for block in blocks:
            buffer += block
            while len(buffer) >= GRAPH_UPLOAD_CHUNK:
                await put(bytes(buffer[:GRAPH_UPLOAD_CHUNK]))
                del buffer[:GRAPH_UPLOAD_CHUNK]
                # httpx responses and requests reference each other; without a young-generation
                # collection every uploaded chunk would stay in memory until the next GC pass
                gc.collect(1)
        if buffer:
            await put(bytes(buffer))
        del buffer
        gc.collect(1)
        if offset != total:
            raise SendError(f"attachment {name}: uploaded {offset} of {total} bytes", retriable=False)

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
            retry_after=parse_retry_after(resp.headers.get("Retry-After")),
        )


# Graph accepts at most 5 entries in internetMessageHeaders ("InvalidInternetMessageHeaderCollection");
# keep the ones most useful for mail rules first.
GRAPH_MAX_CUSTOM_HEADERS = 5
_HEADER_PRIORITY = (
    "x-whatsapp-message-id",
    "x-whatsapp-from",
    "x-whatsapp-crm-match",
    "x-whatsapp-part",
    "x-whatsapp-type",
    "x-whatsapp-bsuid",
)


def graph_custom_headers(message: EmailMessage) -> list[dict[str, str]]:
    candidates = [
        (k, str(v)) for k, v in message.items() if k.lower().startswith("x-") and len(str(v)) < 1000
    ]

    def rank(item: tuple[str, str]) -> int:
        name = item[0].lower()
        return _HEADER_PRIORITY.index(name) if name in _HEADER_PRIORITY else len(_HEADER_PRIORITY)

    chosen = sorted(candidates, key=rank)[:GRAPH_MAX_CUSTOM_HEADERS]
    return [{"name": k, "value": v} for k, v in chosen]


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
