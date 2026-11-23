"""Which WhatsApp templates the "WhatsApp updates" feature sends, and whether Meta lets them be sent.

A template is text approved by Meta in advance, with numbered variables ({{1}}, {{2}} ...) filled in per
message; it is the only kind of message WhatsApp delivers to a customer who has not written in the last 24
hours. Before every send the processor asks :class:`TemplateGuard` whether the template is still usable:

* status APPROVED - only approved templates can be sent, and Meta pauses or disables templates by itself
  (for example after customers block or report them),
* category UTILITY - Meta does not deliver Marketing templates to US numbers, and it may move a template to
  Marketing by itself, so a template that became Marketing must not be used any more,
* as many variables in the text as the settings fill in (otherwise Meta refuses the send, 132000),
* a document header exactly when the template is meant to carry a file.

The answer comes from ``GET /{WA_WABA_ID}/message_templates`` and is kept for RELAY_TEMPLATE_CHECK_SECONDS:
Meta limits how often that list may be read per hour, and a template's state rarely changes.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from app.config import Settings
from app.mail.transports import parse_retry_after
from app.relay.command import flatten
from app.whatsapp.admin import graph_params
from app.whatsapp.send import RETRIABLE_CODES, WhatsAppSendError, explain_error

log = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 20
MAX_PAGES = 5  # requests per check; Meta's name filter returns only a handful of templates
PAGE_SIZE = 100
_FIELDS = "name,status,category,language,components"
# a numbered variable, as Meta stores it in the template text
_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")
# a named variable ({{order_number}}), used by templates created with parameter_format NAMED
_NAMED_PLACEHOLDER = re.compile(r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}")
_NUMERIC_ID = re.compile(r"[0-9]{1,40}")
_STATUS_HINTS = {
    "PENDING": " Meta is still reviewing it (this can take up to 24 hours).",
    "IN_APPEAL": " Meta is reviewing an appeal against its rejection.",
    "REJECTED": " Meta rejected it; WhatsApp Manager shows the reason.",
    "PAUSED": " Meta paused it because of low quality (customers blocked or reported it).",
    "DISABLED": " Meta disabled it after pausing it too many times; a new template is needed.",
}
_MISSING_VALUE = {
    "name": "the customer's name, but it is empty",
    "message": "the message, but it is empty",
    "booking": (
        "a booking number, but the email's subject has none. Write the subject as:"
        " WA | phone number | booking number"
    ),
    "filename": "a file name, but the file has none",
}


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """A template as configured: its name and language at Meta, what fills {{1}}, {{2}} ..., its header."""

    name: str
    language: str
    params: tuple[str, ...]  # tokens from name, message, booking, filename - in the order of {{1}}, {{2}} ...
    header: Literal["none", "document"] = "none"

    def __post_init__(self) -> None:
        # a tuple, so a spec can key the guard's cache even when a list was passed
        object.__setattr__(self, "params", tuple(self.params))
        if self.header not in ("none", "document"):
            raise ValueError(f"template header must be 'none' or 'document', not {self.header!r}")


@dataclass(frozen=True, slots=True)
class TemplateInfo:
    """What Meta says about a template. ``ok`` only when it may be sent; otherwise ``problem`` says why."""

    ok: bool
    status: str | None
    category: str | None
    body_text: str | None  # the approved text with its {{n}} variables (for the confirmation email)
    placeholders: int  # distinct {{n}} variables in the body text
    problem: str | None  # plain English, for the employee's "NOT sent" email and the administrators' alert


class TemplateValueMissing(ValueError):
    """A variable of the template would be empty for this email; WhatsApp refuses empty values (131008)."""

    def __init__(self, token: str, template: str):
        self.token = token
        self.text = f"The WhatsApp template {template} needs {_MISSING_VALUE.get(token, token)}."
        super().__init__(self.text)


class TemplateGuard:
    """Asks Meta whether a template may be sent, keeping each answer for RELAY_TEMPLATE_CHECK_SECONDS."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, clock: Callable[[], float] = time.time):
        self._http = http
        self._s = settings
        self._clock = clock
        self._cache: dict[TemplateSpec, tuple[float, TemplateInfo]] = {}

    async def check(self, spec: TemplateSpec) -> TemplateInfo:
        """Whether ``spec`` may be sent now.

        Raises :class:`WhatsAppSendError` with ``retriable=True`` when Meta could not be asked (network
        error, HTTP 5xx or 429, a rate-limit code): the check is only a read, so it is always safe to repeat.
        Every other outcome is a :class:`TemplateInfo`. Answers read from Meta's list are kept, whether the
        template is usable or not; refusals (for example an expired token) are not, so a fix works at once.
        """
        cached = self._cache.get(spec)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        info, keep = await self._look_up(spec)
        if keep:
            self._cache[spec] = (self._clock() + self._s.relay_template_check_seconds, info)
        else:
            self._cache.pop(spec, None)
        if info.ok:
            log.debug("WhatsApp template %s can be sent", _label(spec))
        else:
            log.warning("template check for %s: %s", _label(spec), info.problem)
        return info

    def forget(self, spec: TemplateSpec | None = None) -> None:
        """Drop the kept answer for ``spec`` (all answers without it), so the next check asks Meta again."""
        if spec is None:
            self._cache.clear()
        else:
            self._cache.pop(spec, None)

    # ------------------------------------------------------------------ helpers
    async def _look_up(self, spec: TemplateSpec) -> tuple[TemplateInfo, bool]:
        """Meta's answer for ``spec``, and whether it may be kept (only answers read from the list)."""
        label = _label(spec)
        token = self._s.wa_access_token
        waba = (self._s.wa_waba_id or "").strip()
        if not spec.name:
            return _not_ok("No WhatsApp template name is set in the settings."), False
        if not token:
            return _not_ok(
                f"WA_ACCESS_TOKEN is not set, so WhatsApp template {label} could not be checked."
            ), False
        if not waba:
            return _not_ok(
                f"WA_WABA_ID is not set, so WhatsApp template {label} could not be checked."
            ), False
        if not _NUMERIC_ID.fullmatch(waba):
            # the id becomes part of an authenticated URL
            return _not_ok("WA_WABA_ID must be the numeric id of the WhatsApp Business account."), False
        secret = self._s.wa_app_secret
        url = httpx.URL(
            f"{self._s.wa_graph_base_url.rstrip('/')}/{self._s.wa_graph_api_version}"
            f"/{quote(waba, safe='')}/message_templates",
            params=graph_params(token, secret, name=spec.name, fields=_FIELDS, limit=str(PAGE_SIZE)),
        )
        origin = _origin(url)
        headers = {"Authorization": f"Bearer {token}"}
        languages: list[str] = []  # other languages the name exists in, for a clearer "not found"
        for page in range(1, MAX_PAGES + 1):
            resp = await self._get(url, headers, label)
            if resp.status_code != 200:
                return _refused(resp, label), False
            body = _json(resp)
            entries = body.get("data")
            if not isinstance(entries, list):
                return _not_ok(
                    f"Meta's answer to the check of WhatsApp template {label} could not be read."
                ), False
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("name") != spec.name:
                    continue  # Meta's name filter also returns templates with similar names
                if entry.get("language") == spec.language:
                    return _evaluate(spec, entry), True
                if isinstance(entry.get("language"), str):
                    languages.append(entry["language"])
            paging = body.get("paging")
            link = paging.get("next") if isinstance(paging, dict) else None
            if not link:
                return _not_found(spec, waba, languages), True
            next_url = _same_origin(link, origin)
            if next_url is None:
                # the access token goes to the configured Graph address only
                problem = (
                    f"WhatsApp template {label} was not on the first {page} page(s) of Meta's template list,"
                    " and the next page was on an unexpected web address, which was not followed."
                )
                return _not_ok(problem), False
            # the token travels in the Authorization header only; the proof has to be on every page
            url = next_url.copy_remove_param("access_token").copy_merge_params(graph_params(token, secret))
        return _not_found(spec, waba, languages, pages=MAX_PAGES), True

    async def _get(self, url: httpx.URL, headers: dict[str, str], label: str) -> httpx.Response:
        try:
            return await self._http.get(url, headers=headers, timeout=CHECK_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise WhatsAppSendError(
                f"could not reach WhatsApp to check template {label}: {exc!r}", retriable=True
            ) from exc


def specs_from_settings(settings: Settings) -> dict[str, TemplateSpec | None]:
    """The configured templates: ``update`` (always), ``booking`` and ``document`` (None when not set up).

    All three share RELAY_TEMPLATE_LANGUAGE. The document template carries the file in its header.
    """
    language = settings.relay_template_language.strip()
    booking_name = settings.relay_booking_template_name.strip()
    document_name = settings.relay_document_template_name.strip()
    return {
        "update": TemplateSpec(
            settings.relay_template_name.strip(),
            language,
            settings.relay_param_list(settings.relay_template_params),
        ),
        "booking": (
            TemplateSpec(
                booking_name, language, settings.relay_param_list(settings.relay_booking_template_params)
            )
            if booking_name
            else None
        ),
        "document": (
            TemplateSpec(
                document_name,
                language,
                settings.relay_param_list(settings.relay_document_template_params),
                header="document",
            )
            if document_name
            else None
        ),
    }


def render_body(body_text: str, values: Sequence[str]) -> str:
    """The template text as the customer sees it: each {{n}} replaced by ``values[n-1]``.

    For the confirmation email ("the customer received: ..."). A number without a value stays as it is, and
    a value that itself contains "{{2}}" is not filled in a second time.
    """

    def fill(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return str(values[index]) if 0 <= index < len(values) else match.group(0)

    return _PLACEHOLDER.sub(fill, body_text or "")


def build_body_params(
    spec: TemplateSpec, *, name: str, message: str, booking: str | None, filename: str | None = None
) -> list[str]:
    """The values for the template's {{1}}, {{2}} ..., in the order the settings name them (``spec.params``).

    Every value is flattened into one line: WhatsApp refuses line breaks, tabs and long runs of spaces in a
    variable (132018). A value that would be empty raises :class:`TemplateValueMissing`, because WhatsApp
    refuses empty variables (131008) - for example a template showing the booking number, for an email whose
    subject has none. Nothing is ever made up to fill the gap.
    """
    available = {"name": name, "message": message, "booking": booking, "filename": filename}
    values: list[str] = []
    for token in spec.params:
        if token not in available:
            raise ValueError(f"WhatsApp template {spec.name}: unknown parameter {token!r}")
        value = flatten(available[token] or "")
        if not value:
            raise TemplateValueMissing(token, spec.name)
        values.append(value)
    return values


# ---------------------------------------------------------------------- reading Meta's answer
def _evaluate(spec: TemplateSpec, entry: dict[str, Any]) -> TemplateInfo:
    status, category = _text(entry.get("status")), _text(entry.get("category"))
    raw = entry.get("components")
    components = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
    body = next((c for c in components if _upper(c.get("type")) == "BODY"), None)
    body_text = body.get("text") if body is not None and isinstance(body.get("text"), str) else None
    placeholders = len(set(_PLACEHOLDER.findall(body_text or "")))
    document_header = any(
        _upper(c.get("type")) == "HEADER" and _upper(c.get("format")) == "DOCUMENT" for c in components
    )
    reasons: list[str] = []
    if _upper(status) != "APPROVED":
        reasons.append(
            f"Its status at Meta is {status or 'not given'}; only APPROVED templates can be sent."
            + _STATUS_HINTS.get(_upper(status), "")
        )
    if _upper(category) != "UTILITY":
        reasons.append(
            f"Its category at Meta is {category or 'not given'}, not UTILITY: Meta does not deliver Marketing"
            " templates to US numbers, so only Utility templates are sent."
        )
    if placeholders != len(spec.params):
        filled = ", ".join(spec.params) or "nothing"
        reason = (
            f"Its text has {placeholders} variable{'' if placeholders == 1 else 's'}, but the settings fill"
            f" {len(spec.params)} ({filled})."
        )
        if _NAMED_PLACEHOLDER.search(body_text or ""):
            reason += (
                " It uses named variables such as {{name}}; this service fills numbered ones ({{1}}, {{2}})."
            )
        reasons.append(reason)
    if spec.header == "document" and not document_header:
        reasons.append("It has no document header, so it cannot carry a file.")
    if spec.header != "document" and document_header:
        reasons.append(
            "It has a document header, so every message would need a file; it cannot be used for text"
            " updates."
        )
    problem = (f"WhatsApp template {_label(spec)} cannot be used. " + " ".join(reasons)) if reasons else None
    return TemplateInfo(
        ok=not reasons,
        status=status,
        category=category,
        body_text=body_text,
        placeholders=placeholders,
        problem=problem,
    )


def _not_found(
    spec: TemplateSpec, waba: str, languages: list[str], *, pages: int | None = None
) -> TemplateInfo:
    where = f"in the first {pages} pages of the template list of" if pages else "in"
    text = (
        f"WhatsApp template {spec.name} in language {spec.language} was not found {where} the WhatsApp"
        f" Business account {waba}."
    )
    others = sorted(set(languages))
    if others:
        text += f" It exists in: {', '.join(others)}. RELAY_TEMPLATE_LANGUAGE has to match one of them."
    else:
        text += (
            " Create it in WhatsApp Manager > Message templates, or correct the template name in the"
            " settings."
        )
    return _not_ok(text)


def _refused(resp: httpx.Response, label: str) -> TemplateInfo:
    """Meta's error answer as a reason; raises for answers that only mean "try again later"."""
    err = _json(resp).get("error")
    err = err if isinstance(err, dict) else {}
    code, subcode = _int(err.get("code")), _int(err.get("error_subcode"))
    data = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
    said = " - ".join(
        str(part).strip().rstrip(".")
        for part in (err.get("message"), data.get("details"))
        if str(part or "").strip()
    )
    # Graph reports its rate limits as HTTP 400 with code 4 or 80007, not only as 429
    if resp.status_code == 429 or resp.status_code >= 500 or code in RETRIABLE_CODES:
        raise WhatsAppSendError(
            f"WhatsApp template check for {label} returned HTTP {resp.status_code}:"
            f" {said or resp.text[:300]}",
            code=code,
            retriable=True,
            status_code=resp.status_code,
            retry_after=parse_retry_after(resp.headers.get("Retry-After")),
        )
    where = f"HTTP {resp.status_code}" + (f", code {code}" if code is not None else "")
    if not err:
        return _not_ok(
            f"Meta's server answered the check of WhatsApp template {label} with {where} and no explanation."
        )
    text = f"Meta refused the check of WhatsApp template {label} ({where}): {said or 'no description given'}."
    hint = _refusal_hint(code, subcode)
    return _not_ok(f"{text} {hint}" if hint else text)


def _refusal_hint(code: int | None, subcode: int | None) -> str | None:
    if code == 190:
        return explain_error(190)
    if code == 10 or (code is not None and 200 <= code <= 299):
        return (
            "The forwarder's WhatsApp access token is not allowed to read the message templates. An"
            " administrator has to give it the whatsapp_business_management permission."
        )
    if code == 100 and subcode == 33:
        return "Check WA_WABA_ID, and that the access token may use that WhatsApp Business account."
    return None


def _not_ok(problem: str) -> TemplateInfo:
    return TemplateInfo(ok=False, status=None, category=None, body_text=None, placeholders=0, problem=problem)


def _label(spec: TemplateSpec) -> str:
    return f"{spec.name} ({spec.language})"


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


def _same_origin(link: object, origin: tuple[str, str, int | None]) -> httpx.URL | None:
    """``paging.next`` as a URL, or None when it is unreadable or points anywhere but the Graph address."""
    try:
        url = httpx.URL(str(link))
    except httpx.InvalidURL:
        return None
    return url if _origin(url) == origin else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _upper(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
