"""Send messages to a WhatsApp user through the Cloud API: approved templates, text, and files.

A template message (``send_template``) is the only kind WhatsApp accepts outside the customer service
window: its fixed text was approved by Meta beforehand and only its variables ({{1}}, {{2}} ...) and an
optional document header are filled in per message. Free-form content (text, files) is only accepted
within 24 hours of the user's last message; outside that window Meta reports error 131047, usually
later in a delivery report rather than in the answer to the send request.

A WhatsApp message can never be recalled, so failures are sorted into three kinds:

* retriable - WhatsApp definitely did not take the message and trying again may work,
* uncertain - the request went out but no clear answer came back (no answer at all, Meta's "unknown
  error", or a server error that does not plainly say "unavailable" or "too many requests"), so it
  may or may not have been sent (never retried: a customer must not get the same message twice),
* permanent - WhatsApp refused it; ``code`` holds Meta's error code.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.mail.transports import parse_retry_after
from app.whatsapp.admin import graph_params

log = logging.getLogger(__name__)

_NUMBER_ID = re.compile(r"[0-9]{1,40}")
# WhatsApp answers a send request within a second or two. The worker also forwards customers'
# messages, so a hanging connection must not hold it for long.
SEND_TIMEOUT_SECONDS = 20
# Meta codes that mean "try again later": API unknown/service errors, rate limits, temporary
# outages, maintenance, and the limit on how fast one user may be messaged.
RETRIABLE_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 131056, 131057, 133004}
# Of those, the ones that plainly say the request was turned away before anything was sent: the
# service is unavailable, overloaded or in maintenance, or a rate limit was hit.
NOT_TAKEN_CODES = {2, 4, 80007, 130429, 131016, 131056, 131057, 133004}
# Meta's "unknown error" codes. A file upload that gets one is simply tried again (nothing reaches the
# customer), but on a send call Meta does not say whether the message went out, so it is never repeated.
UNCERTAIN_SEND_CODES = {1, 131000}
PAIR_RATE_LIMIT = 131056

_EXPLANATIONS = {
    131047: (
        "More than 24 hours had passed since the customer's last WhatsApp message, so WhatsApp no longer"
        " accepted a reply."
    ),
    131026: (
        "WhatsApp could not deliver the message to this customer. They may no longer use WhatsApp on this"
        " number, or their app may be out of date."
    ),
    131042: (
        "There is a billing problem on the WhatsApp Business account (for example, no payment method)."
        " An administrator has to fix it in Meta's Billing Hub."
    ),
    131031: (
        "The WhatsApp Business account is restricted or locked. An administrator has to check it in"
        " WhatsApp Manager."
    ),
    131048: "WhatsApp is limiting messages from the business number because of spam reports or blocks.",
    131053: "WhatsApp could not accept the file (a file type it does not support, or a damaged file).",
    131052: "WhatsApp could not process the file.",
    PAIR_RATE_LIMIT: "Too many messages were sent to this customer in a short time.",
    131021: "The customer's number is the business number itself.",
    131062: "WhatsApp cannot send this kind of message to a customer who has no phone number on record.",
    133010: "The business phone number is not registered with WhatsApp's Cloud API.",
    190: (
        "The forwarder's WhatsApp access token has expired or is no longer valid. An administrator has to"
        " replace WA_ACCESS_TOKEN."
    ),
    10: (
        "The forwarder's WhatsApp access token is not allowed to send messages. An administrator has to"
        " give it the whatsapp_business_messaging permission."
    ),
    # template messages
    132000: (
        "The number of values sent did not match the number of variables ({{1}}, {{2}} ...) in the"
        " WhatsApp template. An administrator has to compare the template in WhatsApp Manager with the"
        " forwarder's settings."
    ),
    132001: (
        "The WhatsApp template does not exist in that language, or Meta has not approved it. An"
        " administrator has to check the template's name, language and status in WhatsApp Manager."
    ),
    132005: (
        "The text is too long for the WhatsApp template once the values are filled in. Shorten the message"
        " and send it again."
    ),
    132007: "WhatsApp refused the message because its content breaks a WhatsApp policy.",
    132012: "A value was not in the format the WhatsApp template expects for that variable.",
    132015: (
        "Meta has paused the WhatsApp template because of low quality (customers blocked or reported it), so"
        " it cannot be sent for now. An administrator can check it in WhatsApp Manager."
    ),
    132016: (
        "Meta has permanently disabled the WhatsApp template after pausing it too many times for low"
        " quality. An administrator has to create a new template."
    ),
    132018: (
        "WhatsApp refused a value in the template: values must not contain line breaks, tabs or more than"
        " four spaces in a row."
    ),
    131008: "A value the WhatsApp template needs was missing or empty.",
    131009: "WhatsApp refused one of the values as not valid.",
    131049: (
        "Meta did not deliver the message, to keep engagement on WhatsApp healthy. For US numbers this"
        " usually means Meta counts the template as Marketing: Marketing messages are not delivered to US"
        " numbers."
    ),
    131050: "The customer has chosen to stop receiving marketing messages from this business on WhatsApp.",
    131051: "WhatsApp does not support this kind of message.",
    131057: (
        "The WhatsApp Business account is in maintenance mode (for example during a throughput upgrade)."
        " Sending works again once Meta has finished."
    ),
    131064: (
        "WhatsApp is limiting the account's messages because Meta found templates in the wrong category."
        " The limit is lifted automatically after the enforcement period; an administrator should check the"
        " templates' categories in WhatsApp Manager."
    ),
}


def explain_error(code: int | None, title: str | None = None, details: str | None = None) -> str:
    """Meta's error in words a member of staff can act on."""
    if code in _EXPLANATIONS:
        return _EXPLANATIONS[code]
    if code is not None and 200 <= code <= 299:
        return _EXPLANATIONS[10]
    parts = [p.strip().rstrip(".") for p in (title, details) if p and p.strip()]
    text = ". ".join(dict.fromkeys(parts)) or "no description given"
    return f"WhatsApp reported an error: {text}" + (f" (code {code})." if code is not None else ".")


class WhatsAppSendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        retriable: bool = False,
        uncertain: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.uncertain = uncertain
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def explanation(self) -> str:
        if self.uncertain:
            return "WhatsApp did not confirm that it took the message, so it may or may not have been sent."
        if self.code is None:
            return f"WhatsApp could not be asked to send it: {self}"
        return explain_error(self.code, str(self))


class WhatsAppClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._s = settings

    # ------------------------------------------------------------------ sending
    async def send_template(
        self,
        number_id: str,
        *,
        phone: str,
        name: str,
        language: str,
        body_params: list[str],
        header_document: dict | None = None,
    ) -> str:
        """Send an approved template to a phone number. Returns the WhatsApp message id.

        ``body_params`` fill the body's {{1}}, {{2}} ... in order; an empty list sends no body component
        (a template without variables). ``header_document`` (``{"id": uploaded media id, "filename": name
        the customer sees}``) fills a document header. WhatsApp refuses empty values (131008) and values
        with line breaks, tabs or more than four spaces in a row (132018), so callers pass flattened text.
        """
        components: list[dict[str, Any]] = []
        if header_document is not None:
            media_id = str(header_document.get("id") or "")
            if not media_id:
                # checked before anything is sent: a header without its file can only be refused
                raise WhatsAppSendError("the file for the template's document header has no upload id")
            document: dict[str, Any] = {"id": media_id}
            if header_document.get("filename"):
                document["filename"] = str(header_document["filename"])
            components.append({"type": "header", "parameters": [{"type": "document", "document": document}]})
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(value)} for value in body_params],
                }
            )
        payload: dict[str, Any] = {
            "type": "template",
            "template": {"name": name, "language": {"code": language}, "components": components},
        }
        return await self._send(number_id, phone, None, payload, None)

    async def send_text(
        self,
        number_id: str,
        *,
        phone: str | None,
        bsuid: str | None,
        body: str,
        reply_to: str | None = None,
    ) -> str:
        """Send a text message (at most 4096 characters). Returns the WhatsApp message id."""
        payload: dict[str, Any] = {"type": "text", "text": {"preview_url": False, "body": body}}
        return await self._send(number_id, phone, bsuid, payload, reply_to)

    async def send_media(
        self,
        number_id: str,
        *,
        phone: str | None,
        bsuid: str | None,
        kind: str,
        media_id: str,
        filename: str | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Send an uploaded file as a ``document``, ``image`` or ``video`` message."""
        media: dict[str, Any] = {"id": media_id}
        if kind == "document" and filename:
            media["filename"] = filename
        return await self._send(number_id, phone, bsuid, {"type": kind, kind: media}, reply_to)

    async def upload_media(self, number_id: str, path: Path, mime_type: str, filename: str) -> str:
        """Upload a file so it can be sent. Safe to repeat: nothing reaches the customer yet."""
        url, params, headers = self._call(number_id, "media")
        try:
            with path.open("rb") as handle:
                resp = await self._http.post(
                    url,
                    params=params,
                    headers=headers,
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files={"file": (filename, handle, mime_type)},
                    timeout=self._s.wa_media_timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise WhatsAppSendError(f"file upload to WhatsApp failed: {exc!r}", retriable=True) from exc
        if resp.status_code != 200:
            raise self._error(resp, "file upload")
        media_id = _json(resp).get("id")
        if not media_id:
            raise WhatsAppSendError("WhatsApp returned no id for the uploaded file", retriable=True)
        return str(media_id)

    # ------------------------------------------------------------------ helpers
    def _call(self, number_id: str, path: str) -> tuple[str, dict[str, str], dict[str, str]]:
        token = self._s.wa_access_token
        if not token:
            raise WhatsAppSendError("WA_ACCESS_TOKEN is not configured", code=190)
        if not _NUMBER_ID.fullmatch(number_id or ""):
            # the id becomes part of an authenticated URL
            raise WhatsAppSendError(f"malformed business phone number id {str(number_id)[:40]!r}")
        url = (
            f"{self._s.wa_graph_base_url.rstrip('/')}/{self._s.wa_graph_api_version}"
            f"/{quote(number_id, safe='')}/{path}"
        )
        return url, graph_params(token, self._s.wa_app_secret), {"Authorization": f"Bearer {token}"}

    async def _send(
        self,
        number_id: str,
        phone: str | None,
        bsuid: str | None,
        payload: dict[str, Any],
        reply_to: str | None,
    ) -> str:
        body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual"}
        digits = "".join(ch for ch in (phone or "") if ch.isdigit())
        if digits:
            body["to"] = "+" + digits
        elif bsuid:
            # a user known only by a business-scoped user id (no phone number in the webhook)
            body["recipient"] = bsuid
        else:
            raise WhatsAppSendError("the customer has neither a phone number nor a WhatsApp user id")
        if reply_to:
            body["context"] = {"message_id": reply_to}  # shows the customer's message as quoted
        body.update(payload)
        url, params, headers = self._call(number_id, "messages")
        try:
            resp = await self._http.post(
                url, params=params, headers=headers, json=body, timeout=SEND_TIMEOUT_SECONDS
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # the request never left: nothing was sent
            raise WhatsAppSendError(f"could not reach WhatsApp: {exc!r}", retriable=True) from exc
        except httpx.HTTPError as exc:
            raise WhatsAppSendError(f"no answer from WhatsApp: {exc!r}", uncertain=True) from exc
        if resp.status_code != 200:
            error = self._error(resp, "send")
            if error.code in UNCERTAIN_SEND_CODES or (
                resp.status_code >= 500 and error.code not in NOT_TAKEN_CODES
            ):
                # Meta's "unknown error" (whatever the HTTP status), or a server or gateway error that
                # does not plainly say "not taken": the message may have gone out all the same, for
                # example when only the answer was lost on the way back.
                error.retriable, error.uncertain = False, True
            raise error
        messages = _json(resp).get("messages")
        wamid = messages[0].get("id") if isinstance(messages, list) and messages else None
        if not wamid:
            raise WhatsAppSendError(
                "WhatsApp accepted the request but returned no message id", uncertain=True
            )
        return str(wamid)

    @staticmethod
    def _error(resp: httpx.Response, step: str) -> WhatsAppSendError:
        err = _json(resp).get("error")
        err = err if isinstance(err, dict) else {}
        try:
            code = int(err["code"])
        except (KeyError, TypeError, ValueError):
            code = None
        data = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
        text = " - ".join(str(p) for p in (err.get("message"), data.get("details")) if p) or resp.text[:300]
        retriable = code in RETRIABLE_CODES or resp.status_code == 429 or resp.status_code >= 500
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        if code == PAIR_RATE_LIMIT and retry_after is None:
            retry_after = 10.0
        return WhatsAppSendError(
            f"WhatsApp {step} returned HTTP {resp.status_code}: {text}",
            code=code,
            retriable=retriable,
            status_code=resp.status_code,
            retry_after=retry_after,
        )


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
