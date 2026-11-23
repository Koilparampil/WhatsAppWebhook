"""Read the relay mailbox through Microsoft Graph (application permissions, no user sign-in).

Employees send their "WhatsApp update" commands (subject ``WA | phone | booking``) to this mailbox.
The service lists what is in its Inbox, reads each command email with its internet headers (which
say who really sent it and how they signed in) and its attachments, downloads the files that are
passed on to the customer, and moves a handled command to one folder. Everything that is not a
handled command stays in the Inbox untouched.

Every call asks for immutable ids (``Prefer: IdType="ImmutableId"``): an email keeps its id when
it is moved, which the queue relies on to handle each email once only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.mail.transports import GRAPH_SCOPE, OAuthTokenProvider, SendError, parse_retry_after

log = logging.getLogger(__name__)

IMMUTABLE_IDS = 'IdType="ImmutableId"'
HTML_BODY = 'outlook.body-content-type="html"'
MESSAGE_FIELDS = (
    "id,internetMessageId,subject,from,sender,toRecipients,ccRecipients,replyTo,receivedDateTime,"
    "hasAttachments,isDraft,body,internetMessageHeaders"
)
# The same without the body: everything that says who sent an email, without a text that may be very long.
MESSAGE_FIELDS_WITHOUT_BODY = ",".join(name for name in MESSAGE_FIELDS.split(",") if name != "body")
# PidTagMessageClass: tells an ordinary email (IPM.Note) from automatic replies, receipts and reports.
MESSAGE_CLASS = "String 0x001A"
ATTACHMENT_FIELDS = "id,name,contentType,size,isInline"
ATTACHMENT_FIELDS_WITH_CID = ATTACHMENT_FIELDS + ",microsoft.graph.fileAttachment/contentId"
# When Graph cannot list content ids, each inline picture up to this size is read once to learn its id.
INLINE_LOOKUP_LIMIT = 8_000_000
INBOX_PAGE = 50
INBOX_PAGES = 10  # at most this many pages (500 emails) are listed in one round
_ADDRESS = re.compile(r"[^@\s<>\",;:\\/]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")  # o'brien@... is an address too
_PICTURE_NAMES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg")


def valid_address(value: str | None) -> str | None:
    """Lower-cased email address, or None if ``value`` is not a plain address."""
    value = (value or "").strip().lower()
    return value if _ADDRESS.fullmatch(value) else None


def _host_of(link: str) -> str:
    try:
        return httpx.URL(link).host or "no host"
    except httpx.InvalidURL:
        return "an unreadable address"


@dataclass(slots=True)
class MailAttachment:
    id: str
    name: str
    content_type: str
    size: int
    is_inline: bool
    content_id: str | None
    kind: str  # "file", "item" (an attached email) or "reference" (a link to a cloud file)


class GraphMailbox:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        oauth: OAuthTokenProvider,
        mailbox: str,
    ):
        self._http = http
        self._s = settings
        self._oauth = oauth
        self.mailbox = mailbox
        self._folders: dict[str, str] = {}
        self._cid_select_works = True

    # ------------------------------------------------------------------ plumbing
    def _url(self, path: str, mailbox: str | None = None) -> str:
        return f"{self._s.graph_base_url.rstrip('/')}/users/{quote(mailbox or self.mailbox, safe='@')}{path}"

    async def _request(
        self, method: str, url: str, *, prefer: str = IMMUTABLE_IDS, **kwargs
    ) -> httpx.Response:
        """One Graph call; a rejected token (401) is replaced once."""
        timeout = kwargs.pop("timeout", 60)
        extra_headers = kwargs.pop("headers", {})
        for attempt in (1, 2):
            token = await self._oauth.get_token(GRAPH_SCOPE)
            headers = {"Authorization": f"Bearer {token}", "Prefer": prefer, **extra_headers}
            try:
                resp = await self._http.request(method, url, headers=headers, timeout=timeout, **kwargs)
            except httpx.HTTPError as exc:
                raise SendError(f"Graph request failed: {exc!r}") from exc
            if resp.status_code != 401 or attempt == 2:
                return resp
            log.warning("Graph rejected the access token (401); retrying once with a new token")
            self._oauth.invalidate(GRAPH_SCOPE)
        return resp  # pragma: no cover

    @staticmethod
    def _fail(resp: httpx.Response, step: str) -> None:
        retriable = resp.status_code >= 500 or resp.status_code in (401, 403, 404, 408, 429)
        raise SendError(
            f"Graph {step} returned HTTP {resp.status_code}: {resp.text[:300]}",
            retriable=retriable,
            status_code=resp.status_code,
            retry_after=parse_retry_after(resp.headers.get("Retry-After")),
        )

    def _message_url(self, message_id: str, suffix: str = "") -> str:
        return self._url(f"/messages/{quote(message_id, safe='')}{suffix}")

    def _is_graph_link(self, link: str) -> bool:
        """Whether a paging link Graph returned is on the configured Graph address (GRAPH_BASE_URL).

        Every request carries the Graph access token, which can read and send mail (under "Model B" of
        docs/setup-microsoft365-email.md, in every mailbox): a link anywhere else, a look-alike host or
        plain http included, is never followed.
        """
        return link.startswith(self._s.graph_base_url.rstrip("/") + "/")

    # ------------------------------------------------------------------ inbox
    async def list_inbox(self, since_iso: str, pages: int = INBOX_PAGES) -> list[dict[str, Any]]:
        """Emails in the Inbox received at or after ``since_iso``, newest first.

        Each comes with its id and receivedDateTime, and with its sender and subject as the mailbox
        lists them. Nothing has checked them yet: the subject only says whether an email looks like
        a command at all, and the sender is only used to name an email in a notice.

        Follows Graph's paging, so emails that stay in the Inbox (anything that is not a handled
        command is never moved) cannot push newer ones out of sight; capped at ``pages`` pages of 50
        per round.
        """
        params: dict[str, str] | None = {
            "$select": "id,receivedDateTime,from,subject",
            "$filter": f"receivedDateTime ge {since_iso}",
            "$orderby": "receivedDateTime desc",
            "$top": str(INBOX_PAGE),
        }
        url = self._url("/mailFolders/inbox/messages")
        found: list[dict[str, Any]] = []
        for _ in range(pages):
            resp = await self._request("GET", url, params=params)
            if resp.status_code != 200:
                self._fail(resp, "list inbox")
            body = resp.json()
            found.extend(m for m in body.get("value", []) if isinstance(m, dict) and m.get("id"))
            url, params = str(body.get("@odata.nextLink") or ""), None  # the link holds the query
            if not self._is_graph_link(url):
                break
        return found

    async def find_by_subject(self, subject: str) -> list[str]:
        """Ids of the emails with exactly this subject, in any folder (for diagnostics, e.g. relay-check)."""
        escaped = subject.replace("'", "''")
        params = {"$filter": f"subject eq '{escaped}'", "$select": "id", "$top": "10"}
        resp = await self._request("GET", self._url("/messages"), params=params)
        if resp.status_code != 200:
            self._fail(resp, "find messages")
        return [str(m["id"]) for m in resp.json().get("value", []) if isinstance(m, dict) and m.get("id")]

    async def get_message(self, message_id: str, *, with_body: bool = True) -> dict[str, Any] | None:
        """The email with its HTML body, internet headers and message class; None if it is gone.

        The internet headers hold Exchange's ``X-MS-Exchange-Organization-AuthAs`` mark, which says
        whether a signed-in internal user sent the email; the message class and the other headers
        tell automatic mail (out-of-office replies, bounces, receipts) from a person's command.

        With ``with_body=False`` the body is left out: everything that decides whether an email can be
        a command of a trusted sender comes without downloading a text that may be megabytes long (anyone
        can send the mailbox an email with a command-shaped subject).
        """
        prefer = f"{IMMUTABLE_IDS}, {HTML_BODY}" if with_body else IMMUTABLE_IDS
        params = {
            "$select": MESSAGE_FIELDS if with_body else MESSAGE_FIELDS_WITHOUT_BODY,
            "$expand": f"singleValueExtendedProperties($filter=id eq '{MESSAGE_CLASS}')",
        }
        resp = await self._request("GET", self._message_url(message_id), params=params, prefer=prefer)
        if resp.status_code == 400:
            # without the message class the headers still identify automatic mail
            log.warning("Graph refused the message-class lookup (%s); reading without it", resp.text[:200])
            params.pop("$expand")
            resp = await self._request("GET", self._message_url(message_id), params=params, prefer=prefer)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            self._fail(resp, "get message")
        return resp.json()

    # ------------------------------------------------------------------ attachments
    async def list_attachments(self, message_id: str) -> list[MailAttachment]:
        """Attachments without their content (which can be large); pictures shown in the body included.

        Only files the employee attached go to the customer. The kind tells an attached file from an
        attached email or a cloud link, and the content id tells a picture pasted into the text from
        a file (Graph does not mark every pasted picture as inline).
        """
        url = self._message_url(message_id, "/attachments")
        select = ATTACHMENT_FIELDS_WITH_CID if self._cid_select_works else ATTACHMENT_FIELDS
        resp = await self._request("GET", url, params={"$select": select, "$top": "100"})
        if resp.status_code == 400 and self._cid_select_works:
            log.warning(
                "Graph refused to list attachment content ids (%s); reading them one by one", resp.text[:200]
            )
            self._cid_select_works = False
            resp = await self._request("GET", url, params={"$select": ATTACHMENT_FIELDS, "$top": "100"})
        found: list[MailAttachment] = []
        for _ in range(10):  # pages
            if resp.status_code != 200:
                self._fail(resp, "list attachments")
            body = resp.json()
            for raw in body.get("value", []):
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                odata_type = str(raw.get("@odata.type") or "").lower()
                kind = (
                    "item"
                    if "itemattachment" in odata_type
                    else "reference"
                    if "reference" in odata_type
                    else "file"
                )
                found.append(
                    MailAttachment(
                        id=str(raw["id"]),
                        name=str(raw.get("name") or ""),
                        content_type=str(raw.get("contentType") or "application/octet-stream"),
                        size=int(raw.get("size") or 0),
                        is_inline=bool(raw.get("isInline")),
                        content_id=str(raw["contentId"]) if raw.get("contentId") else None,
                        kind=kind,
                    )
                )
            next_link = str(body.get("@odata.nextLink") or "")
            if not next_link:
                break
            if not self._is_graph_link(next_link):
                # The access token goes with every request, so it goes to the configured Graph address only.
                log.warning(
                    "Graph's next page of attachments is on another web address (%s); it was not read",
                    _host_of(next_link),
                )
                break
            resp = await self._request("GET", next_link)
        if not self._cid_select_works:
            # The content id says which picture of the email text an attachment is. Graph does not
            # mark every such picture as "inline", so every picture is asked about.
            for attachment in found:
                picture = attachment.content_type.lower().startswith(
                    "image/"
                ) or attachment.name.lower().endswith(_PICTURE_NAMES)
                if (
                    attachment.kind == "file"
                    and (attachment.is_inline or picture)
                    and attachment.size <= INLINE_LOOKUP_LIMIT
                ):
                    attachment.content_id = await self._content_id(message_id, attachment.id)
        return found

    async def _content_id(self, message_id: str, attachment_id: str) -> str | None:
        url = self._message_url(message_id, f"/attachments/{quote(attachment_id, safe='')}")
        resp = await self._request("GET", url)
        if resp.status_code != 200:
            self._fail(resp, "get attachment")
        value = resp.json().get("contentId")
        return str(value) if value else None

    async def download_attachment(self, message_id: str, attachment: MailAttachment, dest: Path) -> int:
        """Stream one attachment to ``dest`` (an attached email arrives as its MIME text). Returns bytes."""
        url = self._message_url(message_id, f"/attachments/{quote(attachment.id, safe='')}/$value")
        token = await self._oauth.get_token(GRAPH_SCOPE)
        headers = {"Authorization": f"Bearer {token}", "Prefer": IMMUTABLE_IDS}
        size = 0
        try:
            async with self._http.stream("GET", url, headers=headers, timeout=120) as resp:
                if resp.status_code == 401:
                    self._oauth.invalidate(GRAPH_SCOPE)
                if resp.status_code != 200:
                    await resp.aread()
                    self._fail(resp, "download attachment")
                with dest.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        await asyncio.to_thread(handle.write, chunk)
                        size += len(chunk)
        except httpx.HTTPError as exc:
            await asyncio.to_thread(dest.unlink, missing_ok=True)
            raise SendError(f"Graph attachment download failed: {exc!r}") from exc
        except SendError:
            await asyncio.to_thread(dest.unlink, missing_ok=True)
            raise
        return size

    # ------------------------------------------------------------------ folders
    async def folder_id(self, name: str) -> str:
        """Id of the top-level folder ``name``, created on first use."""
        if name in self._folders:
            return self._folders[name]
        url = self._url("/mailFolders")
        escaped = name.replace("'", "''")
        params = {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName", "$top": "5"}
        for attempt in (1, 2):
            resp = await self._request("GET", url, params=params)
            if resp.status_code != 200:
                self._fail(resp, "find folder")
            folders = [f for f in resp.json().get("value", []) if isinstance(f, dict) and f.get("id")]
            if folders:
                self._folders[name] = str(folders[0]["id"])
                return self._folders[name]
            if attempt == 2:
                break
            created = await self._request("POST", url, json={"displayName": name})
            if created.status_code in (200, 201) and created.json().get("id"):
                self._folders[name] = str(created.json()["id"])
                return self._folders[name]
            if created.status_code != 409:  # 409: someone created it in the meantime; look again
                self._fail(created, "create folder")
        raise SendError(f"Graph could not find or create the mail folder {name!r}")

    async def move(self, message_id: str, folder_name: str) -> None:
        """Move an email out of the Inbox (a handled command goes to RELAY_FOLDER).

        An email that no longer exists counts as moved.
        """
        folder = await self.folder_id(folder_name)
        resp = await self._request(
            "POST", self._message_url(message_id, "/move"), json={"destinationId": folder}
        )
        if resp.status_code == 404:
            if "folder" in resp.text.lower():
                self._folders.pop(folder_name, None)  # the folder was deleted: look it up again next time
                self._fail(resp, "move message")
            log.info("email %s was already gone when it was to be filed", message_id[:24])
            return
        if resp.status_code not in (200, 201):
            self._fail(resp, "move message")
