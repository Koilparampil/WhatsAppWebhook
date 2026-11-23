"""Handle one employee's "WhatsApp update" email: decide once, send at most once, always report back.

A queue item (KIND_RELAY, key ``relay:<immutable Graph id>``) goes through fixed steps, and the state of
every step is saved in ``relay_state`` before and after its side effect:

1. the decision, made once and stored: ``gone`` (the email no longer exists), ``ignore`` (not a command:
   left in the Inbox untouched and never answered), ``not_allowed`` (shaped like a command but not from a
   trusted sender: left in the Inbox and listed in the administrators' digest), ``refuse`` (a command that
   breaks a rule: the employee is told why), ``dry_run`` or ``send``. Once the sender is verified and the
   command read, the email is kept as ``pending`` (decided anew on the next attempt) while Meta's template
   list and the attachments are looked at: those can fail until the worker gives up, and the employee must
   hear back even then;
2. ``template``: the update template to the customer;
3. ``file:<attachment id>``: each attached file that may go, only after the update itself was sent;
4. ``notice``: the outcome email to the employee (a KIND_NOTE item with a fixed queue key);
5. ``filed``: the email moved to RELAY_FOLDER.

A WhatsApp message can never be taken back, so a step that reaches the customer is never repeated once it
was tried: ``sent``, ``uncertain`` (WhatsApp gave no clear answer), ``refused`` and ``skipped`` are final, and
such a step found in ``sending`` (the service stopped half-way) becomes ``uncertain``. Only WhatsApp's plain
"not taken" answers (rate limits, service unavailable) put a step back to not started, and the error goes to
the worker, which tries the item again later. Retries, crashes and ``replay-failed`` therefore never send
anything twice. Downloading and uploading a file come before its "sending" mark: both can be repeated
freely, as nothing reaches the customer before the send itself. The bookkeeping steps (outcome notice,
filing, template alert) cannot duplicate anything (fixed queue keys, immutable ids), so they are simply
done again when found half-done: the employee must always hear back.

The decision is not made again, but the owner's rules are checked once more right before every send (the
email's age, dry run, test numbers, the allow-list, the template's approval): a send can happen minutes or
hours after the decision, for example after a retry or a restart with changed settings.

When the worker gives up on an email (dead letter), whatever has not gone out yet never will: those steps
are marked skipped and the employee is told so, and ``replay-failed`` only finishes the bookkeeping. An
employee who was told "NOT sent" may have sent the update again; a replay must not send it a second time.
Once the employee's outcome email is queued, nothing more goes to the customer, even when saving the
skipped marks failed at that moment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.config import Settings
from app.crm.directory import LookupResult, LookupStatus
from app.mail.compose import _local_time, format_phone_for_humans
from app.relay import notices
from app.relay.command import (
    CommandRefused,
    looks_like_command,
    normalize_target_phone,
    parse_body,
    parse_subject,
)
from app.relay.files import (
    DOCUMENT,
    NOT_A_FILE,
    PASTED_PICTURES_NOTE,
    PDF_NO_TEMPLATE,
    classify,
    document_template_not_ready,
    is_pasted_picture,
    referenced_cids,
    window_open,
    window_since,
)
from app.relay.html import html_to_text
from app.relay.mailbox import MailAttachment
from app.relay.mailinfo import MailInfo, auth_marks, automatic_mail, internal_auth_ok, subject_edited
from app.relay.templates import (
    TemplateInfo,
    TemplateSpec,
    TemplateValueMissing,
    build_body_params,
    render_body,
    specs_from_settings,
)
from app.store import KIND_NOTE, KIND_RELAY, QueueItem, Store
from app.whatsapp.media import safe_filename
from app.whatsapp.send import WhatsAppSendError

log = logging.getLogger(__name__)

DAY = 24 * 3600
DIGEST_META = "relay_not_allowed"
TEMPLATE_ALERT_META = "relay_template_alert:"
DIGEST_LIMIT = 50  # emails listed in one not-allowed digest; any more are only counted

SENDING, SENT, UNCERTAIN, REFUSED, SKIPPED = "sending", "sent", "uncertain", "refused", "skipped"
COMMAND_ACTIONS = ("refuse", "dry_run", "send")
PENDING = "pending"  # sender verified and command read; not decided yet (made again on the next attempt)
# Meta's answers that say something is wrong with the template itself: its kept check result is dropped
TEMPLATE_ERROR_CODES = {132000, 132001, 132005, 132012, 132015, 132016, 132018}
# Of those, the ones that say the template cannot be used at all until an administrator acts: missing or
# not approved in that language, paused, disabled, or no longer matching the settings. Meta can say so
# while the kept check still says APPROVED; it then counts as a template that is not ready (alerted).
TEMPLATE_UNUSABLE_CODES = {132000, 132001, 132015, 132016}

_HOW = {"media": "as its own WhatsApp message", "template": "with the WhatsApp document template"}
INTERRUPTED = "The service stopped while this was being sent to WhatsApp, so it may or may not have gone out."
GAVE_UP = (
    "WhatsApp or the mailbox could not be reached for a while, so your update was NOT sent. It will not be"
    " sent later on its own."
)
GAVE_UP_SHORT = "WhatsApp or the mailbox could not be reached in time"
GAVE_UP_FILE = "WhatsApp or the mailbox could not be reached for a while, so this file was not sent."
WINDOW_CLOSED = (
    "The customer's 24-hour WhatsApp window closed before the file could be sent, so WhatsApp would not have"
    " taken it."
)
# Subject words and next steps for the processor's own reasons (app.relay.notices knows the others)
_SHORT = {
    "whatsapp_refused": "WhatsApp refused it",
    "dry_run_now": "the service was switched to dry run",
    "not_allowed_now": "your address may no longer send updates",
    "not_finished": "WhatsApp could not be reached in time",
    "value_missing": "a value the WhatsApp template needs is missing",
}
_NEXT = {
    "dry_run_now": (
        "Nothing reaches customers while the service is in dry run. Contact the customer another way if the"
        " update cannot wait."
    ),
    "not_allowed_now": (
        "Ask the administrator whether you should still be able to send WhatsApp updates. Contact the"
        " customer another way in the meantime."
    ),
    "not_finished": (
        "If the customer still needs the update, send it again as a new email in a few minutes, or contact"
        " the customer another way."
    ),
}


class RelayProcessor:
    """Decides what to do with a command email and carries it out, step by step (see the module docstring)."""

    wakeup: asyncio.Event | None = None  # set after every enqueue; the wiring points it at worker.wakeup

    def __init__(
        self,
        settings: Settings,
        store: Store,
        mailbox: Any,
        whatsapp: Any,
        guard: Any,
        directory: Any,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = store
        self.mailbox = mailbox
        self.whatsapp = whatsapp
        self.guard = guard
        self.directory = directory
        self.clock = clock
        self.wakeup = None
        self._counts: dict[str, int] = {}
        self._last_decision_at: float | None = None
        self._messages_sent = 0
        self._last_sent_at: float | None = None

    @property
    def _number_id(self) -> str:
        # Never WA_PHONE_NUMBER_ID directly: setting that one would switch on the forwarder's own filter.
        return self.settings.relay_sending_number_id

    # ------------------------------------------------------------------ the worker's entry points
    async def process(self, item: QueueItem) -> int:
        """Handle one KIND_RELAY item. Returns how many WhatsApp messages this email has sent in all.

        Raises SendError (Graph) and retriable WhatsAppSendError for the worker to try again later;
        everything else that can go wrong with a command ends in a step state and the employee's notice.
        """
        graph_id = _graph_id(item)
        key = item.key
        state = self._load(key)
        self._recover(key, state)
        if _undecided(state["decision"]):
            decision = await self._decide(item, graph_id, state)
            state["decision"] = decision
            self._save(key, state)
            self._counted(item, decision)
        decision = state["decision"]
        action = decision.get("action")
        if action == "not_allowed":
            self._digest_step(key, state)
        elif action in COMMAND_ACTIONS:
            if action == "send":
                self._stop_if_told(key, graph_id, state)
                await self._template_step(item, state)
                await self._file_steps(item, graph_id, state)
            self._alert_step(key, state)
            self._notice_step(key, graph_id, state)
            await self._filed_step(key, graph_id, state)
        return _messages(state)

    async def process_status(self, item: QueueItem) -> None:
        """A KIND_RELAY_STATUS item: WhatsApp reported a message as ``failed``; tell its employee once."""
        payload = item.payload if isinstance(item.payload, dict) else {}
        wamid = str(payload.get("wamid") or "").strip()
        if not wamid or str(payload.get("status") or "").strip().lower() != "failed":
            return
        record = self.store.get_relay_sent(wamid)
        if record is None or record["failure_notified"]:
            return
        state = self.store.get_relay_state(record["item_key"]) or {}
        decision = state.get("decision") or {}
        employee = decision.get("employee") or {}
        if not employee.get("address") or not decision.get("phone"):
            log.warning("relay status #%s: the email behind a failed WhatsApp message is not known", item.id)
            return
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        error = next((e for e in errors if isinstance(e, dict)), {})
        what = str(record.get("what") or "")
        filename = what[len("file:") :] if what.startswith("file:") else None
        command = decision.get("command") or {}
        message = notices.build_delivery_failed_notice(
            self.settings,
            employee_address=employee["address"],
            employee_name=employee.get("name") or None,
            command_message_id=command.get("message_id") or None,
            command_subject=command.get("subject") or None,
            phone=decision["phone"],
            booking=decision.get("booking"),
            filename=filename,
            template_name=None if filename else (decision.get("template") or {}).get("name"),
            error_code=_int(error.get("code")),
            error_title=_text(error.get("title")),
            error_details=_text(error.get("details")) or _text(error.get("message")),
            sent_at=record.get("created_at"),
            now=self.clock(),
        )
        # the note first: if marking fails, a repeat finds the note already queued under the same key
        self._enqueue_note(f"note:relay_status:{wamid}", message)
        self.store.mark_relay_failure_notified(wamid)
        log.warning(
            "WhatsApp could not deliver %s to %s (code %s); %s was told",
            "a file" if filename else "an update",
            self._mask(decision["phone"]),
            error.get("code"),
            employee["address"],
        )

    def on_dead_letter(self, item: QueueItem, error: str) -> None:
        """The worker gave up on a relay item: say what did and did not go out. Never raises.

        Nothing more is sent for the email afterwards: every customer step that has not happened is marked
        skipped, and the employee is told it was not sent, so a later ``replay-failed`` only finishes the
        bookkeeping. Otherwise an employee who resent the update would find the customer getting it twice.
        Each of its writes is tried even when an earlier one failed, and calling it again for the same item
        queues nothing twice.
        """
        try:
            self._dead_letter(item, error)
        except Exception:
            log.exception("could not report the dead-lettered relay item #%s", getattr(item, "id", "?"))

    def flush_digests(self) -> None:
        """Send the collected not-allowed digest once 24 hours have passed since the last. Never raises."""
        try:
            data = self._load_digest()
            now = self.clock()
            if (data["pending"] or data["more"]) and _due(data["last_alert_at"], now):
                self._send_digest(data["pending"], data["more"], now)
                self._save_digest({"last_alert_at": now, "pending": [], "more": 0})
        except Exception:
            log.exception("could not send the digest of emails from senders who may not send updates")

    def status(self) -> dict[str, Any]:
        """A small summary for /healthz. Never raises."""
        try:
            waiting: int | None = len(self._load_digest()["pending"])
        except Exception:
            waiting = None
        return {
            "decisions": dict(self._counts),
            "last_decision_at": self._last_decision_at,
            "messages_sent": self._messages_sent,
            "last_sent_at": self._last_sent_at,
            "not_allowed_waiting": waiting,
        }

    # ------------------------------------------------------------------ state
    def _load(self, key: str) -> dict[str, Any]:
        # An unreadable state raises (in the store) rather than starting over: that could send twice.
        state = self.store.get_relay_state(key)
        if state is None:
            return {"decision": None, "steps": {}}
        if not isinstance(state.get("steps", {}), dict):
            raise ValueError(f"the saved relay state of {key} has no readable steps")
        state.setdefault("decision", None)
        state.setdefault("steps", {})
        return state

    def _save(self, key: str, state: dict[str, Any]) -> None:
        self.store.save_relay_state(key, state, self.clock())

    def _recover(self, key: str, state: dict[str, Any]) -> None:
        """Steps left in "sending" by a crash: a WhatsApp send (or the digest) may have happened already."""
        changed = False
        for step_id, step in state["steps"].items():
            if step.get("status") == SENDING and _never_repeated(step_id):
                state["steps"][step_id] = {"status": UNCERTAIN, "error": INTERRUPTED, "at": step.get("at")}
                changed = True
                log.warning("relay %s: step %s was interrupted; it is not repeated", _short_key(key), step_id)
        if changed:
            self._save(key, state)

    def _try_save(self, key: str, state: dict[str, Any]) -> bool:
        """Save the state; on failure log it and say so (for the dead letter, which must go on)."""
        try:
            self._save(key, state)
        except Exception:
            log.exception("could not save the state of relay %s", _short_key(key))
            return False
        return True

    def _stop_if_told(self, key: str, graph_id: str, state: dict[str, Any]) -> None:
        """Nothing more goes to the customer once the employee's outcome email is queued.

        That email is queued only after every customer step is final, or when the worker gave up on the
        email (the employee is then told it was NOT sent). Should saving the dead letter's skipped marks
        have failed, this keeps a replay from sending what the employee was told was not sent.
        """
        decision, steps = state["decision"], state["steps"]
        if not _customer_steps_left(decision, steps) or self.store.get(_result_key(graph_id)) is None:
            return
        _finish(decision, steps, self.clock())
        self._save(key, state)
        log.warning("relay %s: its outcome was told already; nothing more is sent", _short_key(key))

    # ------------------------------------------------------------------ the decision (made once)
    async def _decide(self, item: QueueItem, graph_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """What to do with the email: the checks of the spec's decision procedure, first failure wins.

        Only reads (Graph, Meta's template list, the CRM, the store): every side effect is a later step.
        The one write keeps a verified command as ``pending`` before the checks that ask Meta and the
        mailbox, so that a dead letter can still tell the employee (see :meth:`_dead_letter`).

        The email is read without its text first: who sent it is decided from the sender, the subject and
        the internet headers alone, so a stranger's email with a command-shaped subject costs one small read
        however long its text is. Only an email that passes is read in full, and checked again as read then
        (it can have been changed in between).
        """
        now = self.clock()
        raw = await self.mailbox.get_message(graph_id, with_body=False)
        if raw is None:
            return {"action": "gone", "decided_at": now}
        screened = self._screen(item, now, MailInfo.from_graph(raw))
        if screened is not None:
            return screened
        raw = await self.mailbox.get_message(graph_id)
        if raw is None:
            return {"action": "gone", "decided_at": now}
        mail = MailInfo.from_graph(raw)
        screened = self._screen(item, now, mail)
        if screened is not None:
            return screened
        received = _received_at(item, mail)
        tests = self.settings.relay_test_number_list
        decision: dict[str, Any] = {
            "action": "refuse",
            "decided_at": now,
            "employee": {"address": mail.author, "name": mail.author_name},
            "command": {
                "message_id": mail.internet_message_id,
                "subject": mail.subject,
                "received_at": received,
            },
            "phone": None,
            "booking": None,
            "test_mode": bool(tests),
            "dry_run": self.settings.relay_dry_run,
        }

        def refuse(code: str, text: str, **extra: Any) -> dict[str, Any]:
            return {
                **decision,
                "action": "refuse",
                "refusal": {"code": code, "text": text, "short": _SHORT.get(code)},
                **extra,
            }

        if now - received > self.settings.relay_max_age_hours * 3600:
            return refuse("too_old", self._too_old_text(received))
        try:
            subject = parse_subject(mail.subject)
            decision["booking"] = subject.booking
            phone = normalize_target_phone(subject.phone_raw, self.settings.relay_default_region)
            decision["phone"] = phone
            # Reading a long email's text is seconds of pure Python work: done in a thread, so that the
            # webhook keeps answering Meta (it carries customers' messages) in the meantime.
            text = mail.body
            if mail.body_is_html:
                text = await asyncio.to_thread(html_to_text, mail.body)
            body = await asyncio.to_thread(
                parse_body,
                text,
                max_name=self.settings.relay_max_name_chars,
                max_message=self.settings.relay_max_message_chars,
                fallback_name=self.settings.relay_name_fallback,
            )
        except CommandRefused as exc:
            return refuse(exc.code, exc.text)
        if tests and phone not in tests:
            return refuse(
                "test_mode",
                "Test mode is on: updates can only be sent to the test numbers the administrator set up, and"
                f" {format_phone_for_humans(phone)} is not one of them.",
            )
        # A verified employee's readable command: from here on the employee hears back whatever happens.
        # Meta's template list and the mailbox's attachments can fail until the worker gives up.
        state["decision"] = {**decision, "action": PENDING}
        self._save(item.key, state)
        specs = specs_from_settings(self.settings)
        spec = specs["booking"] if subject.booking and specs["booking"] else specs["update"]
        assert spec is not None
        decision["template"] = _spec_dict(spec)
        info = await self.guard.check(spec)
        if not info.ok:
            problem = info.problem or f"WhatsApp template {spec.name} cannot be used."
            refused = refuse("template_not_ready", problem)
            refused["refusal"]["template"] = spec.name
            return refused
        try:
            params = build_body_params(spec, name=body.name, message=body.message, booking=subject.booking)
        except TemplateValueMissing as exc:
            return refuse("value_missing", exc.text)
        notes: list[str] = []
        if subject.booking and spec is specs["update"]:
            notes.append(
                f"The booking number {subject.booking} is only shown in this email: the customer's message"
                " does not contain it (no WhatsApp template for updates with a booking number is set up yet)."
            )
        decision.update(
            params=params,
            customer_text=render_body(info.body_text, params) if info.body_text else None,
            name_was_empty=body.name_was_empty,
            lines_joined=body.lines_joined,
        )
        await self._plan_files(graph_id, mail, decision, body.name, body.message, specs["document"], notes)
        decision["crm_match"] = await self._crm_match(phone)
        decision["notes"] = notes
        decision["action"] = "dry_run" if self.settings.relay_dry_run else "send"
        return decision

    def _screen(self, item: QueueItem, now: float, mail: MailInfo) -> dict[str, Any] | None:
        """Checks b to e of the decision procedure; None when ``mail`` can be a trusted employee's command.

        They need nothing but the sender, the subject and the internet headers (never the email's text).
        """
        if not looks_like_command(mail.subject):
            return _ignore(now, mail, "the subject does not start with WA |")
        automatic = automatic_mail(mail)
        if automatic:
            return _ignore(now, mail, automatic)
        if not mail.author:
            return _ignore(now, mail, "the sender's address could not be read")
        if mail.author == self.settings.relay_mailbox_address:
            return _ignore(now, mail, "it came from the relay mailbox itself")
        doubts = self._not_allowed(mail)
        if doubts:
            return {
                "action": "not_allowed",
                "decided_at": now,
                "from": mail.author,
                "received_at": _received_at(item, mail),
                "why": "; ".join(doubts),
            }
        return None

    def _not_allowed(self, mail: MailInfo) -> list[str]:
        """Why the email cannot be taken as a command from a trusted employee (owner decision 2)."""
        doubts: list[str] = []
        if not internal_auth_ok(mail, self.settings.relay_require_internal_auth):
            marks = [mark[:40] for mark in auth_marks(mail)]
            if marks:
                doubts.append(
                    "Exchange did not mark it as sent by a signed-in user of the organisation (AuthAs: "
                    + ", ".join(mark or "empty" for mark in marks)
                    + ")"
                )
            else:
                doubts.append("Exchange's sign-in mark (X-MS-Exchange-Organization-AuthAs) is missing")
        if mail.on_behalf:
            doubts.append("it was sent on behalf of someone else (From and Sender differ)")
        if mail.author not in self.settings.relay_allowed_sender_list:
            doubts.append("the sender is not on RELAY_ALLOWED_SENDERS")
        if subject_edited(mail):
            doubts.append(
                "its subject is not the one it arrived with (the email was edited in the mailbox after it"
                " arrived)"
            )
        if mail.is_draft:
            doubts.append("it is a draft, written in the mailbox rather than sent to it")
        return doubts

    async def _plan_files(
        self,
        graph_id: str,
        mail: MailInfo,
        decision: dict[str, Any],
        name: str,
        message: str,
        document_spec: TemplateSpec | None,
        notes: list[str],
    ) -> None:
        """How each attached file would go (owner decision 18 and the spec's section 9)."""
        attachments = await self.mailbox.list_attachments(graph_id)
        shown = referenced_cids(mail.body) if mail.body_is_html else set()
        files: list[dict[str, Any]] = []
        real: list[tuple[dict[str, Any], MailAttachment]] = []
        pasted = 0
        for index, attachment in enumerate(attachments):
            entry: dict[str, Any] = {
                "id": attachment.id,
                "n": index,
                "name": safe_filename(attachment.name, f"attachment-{index + 1}"),
                "type": attachment.content_type,
                "size": int(attachment.size or 0),
                "kind": None,
                "mime": None,
                "route": None,
                "reason": None,
                "params": None,
            }
            if attachment.kind != "file":
                entry["reason"] = NOT_A_FILE
                files.append(entry)
            elif is_pasted_picture(
                attachment.is_inline,
                attachment.content_id,
                shown,
                name=attachment.name,
                content_type=attachment.content_type,
            ):
                pasted += 1  # a picture that is part of the text, not a file: never sent, mentioned in a note
            else:
                files.append(entry)
                real.append((entry, attachment))
        is_open: bool | None = self._window_open(decision["phone"]) if real else None
        document_info: TemplateInfo | None = None
        for entry, attachment in real:
            verdict = classify(
                attachment.name, attachment.content_type, entry["size"], window_open=bool(is_open)
            )
            entry.update(kind=verdict.kind, mime=verdict.mime, reason=verdict.reason)
            if verdict.route == "media":
                entry["route"] = "media"
                continue
            if verdict.route != "document_template":
                continue
            if document_spec is None:
                entry["reason"] = PDF_NO_TEMPLATE
                continue
            if document_info is None:  # asked once per email, and only when a file needs it
                decision["document_template"] = _spec_dict(document_spec)
                document_info = await self.guard.check(document_spec)
                if not document_info.ok:
                    decision["document_problem"] = document_info.problem or "no reason given"
            if not document_info.ok:
                entry["reason"] = document_template_not_ready(document_info.problem)
                continue
            try:
                params = build_body_params(
                    document_spec,
                    name=name,
                    message=message,
                    booking=decision.get("booking"),
                    filename=entry["name"],
                )
            except TemplateValueMissing as exc:
                entry["reason"] = exc.text
                continue
            entry.update(route="template", params=params)
        decision["files"] = files
        decision["window_open"] = is_open
        if pasted:
            notes.append(PASTED_PICTURES_NOTE)

    def _window_open(self, phone: str) -> bool:
        """Whether the customer wrote to the sending number recently enough for free-form messages."""
        now = self.clock()
        last = self.store.recent_inbound_timestamp(
            phone.lstrip("+"), since=window_since(now), phone_number_id=self._number_id or None
        )
        return window_open(last, now)

    async def _crm_match(self, phone: str) -> str:
        """One line about the number in VShipCRM, for the employee. Never blocks sending."""
        if self.directory is None:
            return "CRM lookup is switched off"
        try:
            result = await self.directory.lookup(phone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CRM lookup for %s failed: %s", self._mask(phone), type(exc).__name__)
            return f"CRM lookup failed: {type(exc).__name__}"
        return crm_match_text(result)

    # ------------------------------------------------------------------ steps that reach the customer
    async def _template_step(self, item: QueueItem, state: dict[str, Any]) -> None:
        key, steps = item.key, state["steps"]
        if "template" in steps:
            return
        decision = state["decision"]
        spec = _spec(decision["template"])
        blocked = self._blocked(decision)
        if blocked is None:
            info = await self.guard.check(spec)  # "not taken" errors go to the worker: nothing was sent
            if not info.ok:
                blocked = (
                    "template_not_ready",
                    info.problem or f"WhatsApp template {spec.name} cannot be used.",
                )
        if blocked is not None:
            code, text = blocked
            steps["template"] = {
                "status": REFUSED,
                "reason_code": code,
                "error": text,
                "template": spec.name,
                "at": self.clock(),
            }
            self._save(key, state)
            log.warning("relay #%s: the update was not sent (%s)", item.id, code)
            return
        steps["template"] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        try:
            wamid = await self.whatsapp.send_template(
                self._number_id,
                phone=decision["phone"],
                name=spec.name,
                language=spec.language,
                body_params=list(decision["params"]),
            )
        except WhatsAppSendError as exc:
            if exc.code in TEMPLATE_ERROR_CODES:
                self.guard.forget(spec)  # the next email asks Meta again
            if self._failed(item, state, "template", exc, template=spec.name):
                raise
            return
        self._sent(key, state, "template", wamid, "update")
        log.info("relay #%s: update sent to %s (%s)", item.id, self._mask(decision["phone"]), wamid)

    async def _file_steps(self, item: QueueItem, graph_id: str, state: dict[str, Any]) -> None:
        decision, steps = state["decision"], state["steps"]
        todo = [f for f in decision.get("files") or [] if f.get("route") and _file_step(f) not in steps]
        if not todo:
            return
        if (steps.get("template") or {}).get("status") != SENT:
            for entry in todo:  # files only follow an update that was sent
                steps[_file_step(entry)] = {"status": SKIPPED, "error": None, "at": self.clock()}
            self._save(item.key, state)
            return
        spool = Path(self.settings.spool_dir) / f"relay_{_safe_id(graph_id)}"
        try:
            for entry in todo:
                await self._send_file(item, graph_id, state, entry, spool)
        finally:
            await asyncio.to_thread(shutil.rmtree, spool, ignore_errors=True)

    async def _send_file(
        self, item: QueueItem, graph_id: str, state: dict[str, Any], entry: dict[str, Any], spool: Path
    ) -> None:
        key, steps, decision = item.key, state["steps"], state["decision"]
        step_id = _file_step(entry)
        blocked = self._blocked(decision)
        if blocked is None and entry["route"] == "media" and not self._window_open(decision["phone"]):
            blocked = ("window_closed", WINDOW_CLOSED)
        spec: TemplateSpec | None = None
        if blocked is None and entry["route"] == "template":
            spec = _spec(decision["document_template"])
            info = await self.guard.check(spec)
            if not info.ok:
                steps[step_id] = {
                    "status": REFUSED,
                    "reason_code": "template_not_ready",
                    "template": spec.name,
                    "error": document_template_not_ready(info.problem),
                    "at": self.clock(),
                }
                self._save(key, state)
                return
        if blocked is not None:
            steps[step_id] = {
                "status": SKIPPED,
                "reason_code": blocked[0],
                "error": blocked[1],
                "at": self.clock(),
            }
            self._save(key, state)
            return
        # Download and upload can be repeated freely: nothing reaches the customer before the send itself.
        # The file on disk never carries the sender's file name.
        await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True)
        path = spool / f"{int(entry['n'])}.bin"
        attachment = MailAttachment(
            id=entry["id"],
            name=entry["name"],
            content_type=entry["type"],
            size=int(entry["size"]),
            is_inline=False,
            content_id=None,
            kind="file",
        )
        await self.mailbox.download_attachment(graph_id, attachment, path)
        try:
            media_id = await self.whatsapp.upload_media(self._number_id, path, entry["mime"], entry["name"])
        except WhatsAppSendError as exc:
            if exc.retriable:
                raise
            steps[step_id] = {
                "status": REFUSED,
                "reason_code": "upload_refused",
                "error_code": exc.code,
                "error": exc.explanation,
                "at": self.clock(),
            }
            self._save(key, state)
            return
        steps[step_id] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        try:
            if spec is None:
                wamid = await self.whatsapp.send_media(
                    self._number_id,
                    phone=decision["phone"],
                    bsuid=None,
                    kind=entry["kind"],
                    media_id=media_id,
                    filename=entry["name"] if entry["kind"] == DOCUMENT else None,
                )
            else:
                wamid = await self.whatsapp.send_template(
                    self._number_id,
                    phone=decision["phone"],
                    name=spec.name,
                    language=spec.language,
                    body_params=list(entry.get("params") or []),
                    header_document={"id": media_id, "filename": entry["name"]},
                )
        except WhatsAppSendError as exc:
            if spec is not None and exc.code in TEMPLATE_ERROR_CODES:
                self.guard.forget(spec)
            if self._failed(item, state, step_id, exc, template=spec.name if spec else None):
                raise
            return
        self._sent(key, state, step_id, wamid, f"file:{entry['name']}")
        log.info("relay #%s: %s sent as %s (%s)", item.id, step_id, entry["route"], wamid)

    def _blocked(self, decision: dict[str, Any]) -> tuple[str, str] | None:
        """Why nothing (more) may go to the customer now, although the decision allowed it."""
        now = self.clock()
        received = float(decision["command"]["received_at"])
        if now - received > self.settings.relay_max_age_hours * 3600:
            hours = f"{self.settings.relay_max_age_hours:g}"
            return "too_old", (
                f"Your email is now more than {hours} hours old, so it was not sent: the information may be"
                " out of date by now."
            )
        if self.settings.relay_dry_run:
            return "dry_run_now", (
                "The WhatsApp update service was switched to dry run (test operation) before it went out, so"
                " it was not sent."
            )
        tests = self.settings.relay_test_number_list
        if tests and decision["phone"] not in tests:
            number = format_phone_for_humans(decision["phone"])
            return "test_mode", (
                f"Test mode was switched on before it went out, and {number} is not a test number, so it was"
                " not sent."
            )
        if decision["employee"]["address"] not in self.settings.relay_allowed_sender_list:
            return "not_allowed_now", (
                "Your address was taken off the list of people who may send WhatsApp updates before it went"
                " out, so it was not sent."
            )
        return None

    def _failed(
        self,
        item: QueueItem,
        state: dict[str, Any],
        step_id: str,
        exc: WhatsAppSendError,
        *,
        template: str | None = None,
    ) -> bool:
        """Record a send error. True: WhatsApp plainly did not take it; the caller re-raises for a retry.

        ``template`` names the template of a template send: when Meta says that template cannot be used
        at all, the step is recorded like a template the check found not ready, so ALERT_TO is alerted.
        """
        steps = state["steps"]
        if exc.uncertain:
            steps[step_id] = {"status": UNCERTAIN, "error": exc.explanation, "at": self.clock()}
            self._save(item.key, state)
            log.warning("relay #%s: %s may or may not have been sent (%s)", item.id, step_id, exc)
            return False
        if exc.retriable:
            steps.pop(step_id, None)  # definitely not sent: tried again when the worker retries the item
            self._save(item.key, state)
            log.warning("relay #%s: WhatsApp did not take %s for now (%s)", item.id, step_id, exc)
            return True
        step: dict[str, Any] = {
            "status": REFUSED,
            "reason_code": "whatsapp_refused",
            "error_code": exc.code,
            "error": exc.explanation,
            "at": self.clock(),
        }
        if template is not None and exc.code in TEMPLATE_UNUSABLE_CODES:
            # Paused, disabled or no longer approved since the kept check: owner decision 8 alerts ALERT_TO.
            step.update(
                reason_code="template_not_ready",
                template=template,
                problem=(
                    f"Meta refused to send a message with WhatsApp template {template} (error code"
                    f" {exc.code}): {exc.explanation}"
                ),
            )
        steps[step_id] = step
        self._save(item.key, state)
        log.warning("relay #%s: WhatsApp refused %s (code %s)", item.id, step_id, exc.code)
        return False

    def _sent(self, key: str, state: dict[str, Any], step_id: str, wamid: str, what: str) -> None:
        now = self.clock()
        # Recorded first: should the state write fail, a later "failed" status can still be reported.
        self.store.record_relay_sent(wamid, key, what, now)
        state["steps"][step_id] = {"status": SENT, "wamid": wamid, "at": now}
        self._save(key, state)
        self._messages_sent += 1
        self._last_sent_at = now

    # ------------------------------------------------------------------ bookkeeping steps
    def _digest_step(self, key: str, state: dict[str, Any]) -> None:
        steps = state["steps"]
        if "digest" in steps:  # adding to the digest again would list the email twice
            return
        decision = state["decision"]
        steps["digest"] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        self._note_not_allowed(
            {"at": decision.get("received_at"), "from": decision.get("from"), "why": decision.get("why")}
        )
        steps["digest"] = {"status": SENT, "at": self.clock()}
        self._save(key, state)

    def _alert_step(self, key: str, state: dict[str, Any]) -> None:
        problems = _template_problems(state)
        steps = state["steps"]
        if not problems or (steps.get("alert") or {}).get("status") == SENT:
            return
        steps["alert"] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        for name, problem in problems.items():
            self._template_alert(name, problem)
        steps["alert"] = {"status": SENT, "at": self.clock()}
        self._save(key, state)

    def _notice_step(self, key: str, graph_id: str, state: dict[str, Any]) -> None:
        steps = state["steps"]
        if (steps.get("notice") or {}).get("status") == SENT:
            return
        steps["notice"] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        self._enqueue_note(_result_key(graph_id), self._outcome(state))
        steps["notice"] = {"status": SENT, "at": self.clock()}
        self._save(key, state)

    async def _filed_step(self, key: str, graph_id: str, state: dict[str, Any]) -> None:
        steps = state["steps"]
        if (steps.get("filed") or {}).get("status") == SENT:
            return
        steps["filed"] = {"status": SENDING, "at": self.clock()}
        self._save(key, state)
        await self.mailbox.move(graph_id, self.settings.relay_folder)
        steps["filed"] = {"status": SENT, "at": self.clock()}
        self._save(key, state)

    def _enqueue_note(self, key: str, message: EmailMessage) -> bool:
        added = self.store.enqueue(KIND_NOTE, key, notices.to_payload(message), self.clock())
        if self.wakeup is not None:
            self.wakeup.set()
        return added

    # ------------------------------------------------------------------ the employee's outcome email
    def _outcome(self, state: dict[str, Any]) -> EmailMessage:
        decision, steps = state["decision"], state["steps"]
        employee, command = decision["employee"], decision["command"]
        template = decision.get("template") or {}
        common: dict[str, Any] = {
            "employee_address": employee["address"],
            "employee_name": employee.get("name") or None,
            "command_message_id": command.get("message_id") or None,
            "command_subject": command.get("subject") or None,
            "now": self.clock(),
        }
        if decision["action"] == "refuse":
            refusal = decision["refusal"]
            return notices.build_refused_notice(
                self.settings,
                **common,
                refusal_code=refusal["code"],
                refusal_text=refusal["text"],
                short_reason=refusal.get("short"),
                next_step=_NEXT.get(refusal["code"]),
                phone=decision.get("phone"),
                booking=decision.get("booking"),
                template_name=template.get("name"),
                template_language=template.get("language"),
                test_mode=bool(decision.get("test_mode")),
                dry_run=bool(decision.get("dry_run")),
            )
        details: dict[str, Any] = {
            "phone": decision["phone"],
            "booking": decision.get("booking"),
            "crm_match": decision.get("crm_match"),
            "template_name": template.get("name"),
            "template_language": template.get("language"),
            "notes": decision.get("notes") or [],
            "test_mode": bool(decision.get("test_mode")),
        }
        texts: dict[str, Any] = {
            "customer_text": decision.get("customer_text"),
            "parameters": decision.get("params") or [],
            "name_fallback_used": bool(decision.get("name_was_empty")),
            "lines_joined": bool(decision.get("lines_joined")),
        }
        if decision["action"] == "dry_run":
            return notices.build_dry_run_notice(
                self.settings,
                **common,
                **details,
                **texts,
                files=_file_results(decision, steps, dry_run=True),
            )
        step = steps.get("template") or {}
        files = _file_results(decision, steps)
        if step.get("status") == SENT:
            return notices.build_sent_notice(
                self.settings, **common, **details, **texts, files=files, sent_at=step.get("at")
            )
        if step.get("status") == UNCERTAIN:
            return notices.build_uncertain_notice(
                self.settings,
                **common,
                **details,
                **texts,
                files=files,
                uncertainty=step.get("error"),
                sent_at=step.get("at"),
            )
        # refused or skipped: nothing reached the customer
        code = step.get("reason_code") or "not_finished"
        # WhatsApp itself said no (a template Meta no longer allows keeps its own code and next step)
        whatsapp = code == "whatsapp_refused" or step.get("error_code") is not None
        reason = "WhatsApp refused to send your update." if whatsapp else step.get("error") or GAVE_UP
        return notices.build_refused_notice(
            self.settings,
            **common,
            **details,
            refusal_code=code,
            refusal_text=reason,
            short_reason=_SHORT.get(code),
            next_step=_NEXT.get(code),
            error_code=step.get("error_code"),
            error_explanation=step.get("error") if whatsapp else None,
            files=files,
            dry_run=bool(decision.get("dry_run")),
        )

    # ------------------------------------------------------------------ alerts for the administrators
    def _template_alert(self, name: str, problem: str | None) -> None:
        """At most one alert per template per 24 hours (meta relay_template_alert:<name>)."""
        meta = TEMPLATE_ALERT_META + name
        now = self.clock()
        if not _due(_float(self.store.get_meta(meta)), now):
            return
        lines = [
            problem or f"WhatsApp template {name} cannot be used.",
            "Until it is fixed nothing is sent with this template: employees who send a WhatsApp update are"
            " told it was NOT sent (or, for files, that the file was not sent).",
            "Check the template in WhatsApp Manager > Message templates, then run:"
            " whatsapp-webhook relay-check",
            "This alert is sent at most once a day per template.",
        ]
        message = notices.build_relay_alert(
            f"WhatsApp template {name} cannot be used", lines, self.settings, now=now
        )
        self._enqueue_note(_alert_key(f"template:{name}", int(now // DAY)), message)
        self.store.set_meta(meta, repr(now))

    def _note_not_allowed(self, entry: dict[str, Any]) -> None:
        """List a command-shaped email from a sender who may not send updates (owner decision 17).

        The first one is reported at once; later ones are collected and listed in the next alert, at most
        one per 24 hours (see :meth:`flush_digests`).
        """
        data = self._load_digest()
        now = self.clock()
        if _due(data["last_alert_at"], now):
            self._send_digest([*data["pending"], entry], data["more"], now)
            data = {"last_alert_at": now, "pending": [], "more": 0}
        elif len(data["pending"]) < DIGEST_LIMIT:
            data["pending"].append(entry)
        else:
            data["more"] += 1
        self._save_digest(data)

    def _send_digest(self, entries: list[dict[str, Any]], more: int, now: float) -> None:
        listed, more = entries[:DIGEST_LIMIT], more + max(0, len(entries) - DIGEST_LIMIT)
        mailbox = self.settings.relay_mailbox_address or "the WhatsApp updates mailbox"
        lines = [
            f"These emails to {mailbox} looked like WhatsApp update commands (the subject starts with WA |)"
            " but were not accepted. Nothing was sent to WhatsApp, the senders were not answered, and the"
            " emails were left in the Inbox untouched:",
            *(
                f"- {_local_time(int(_float(e.get('at')) or 0), self.settings.mail_timezone)}:"
                f" {e.get('from') or '(no address)'} - {e.get('why') or 'not accepted'}"
                for e in listed
            ),
            *([f"- ... and {more} more."] if more else []),
            "Only addresses on RELAY_ALLOWED_SENDERS may send updates, and only from their own mailbox while"
            " signed in (Exchange then marks the email AuthAs: Internal). If a sender should be allowed, add"
            " the address to RELAY_ALLOWED_SENDERS. If you do not recognise a sender, someone may be trying"
            " to send WhatsApp messages in VShip's name: look at the email in the Inbox.",
            "At most one such alert is sent per 24 hours; emails that arrive in between are listed in the"
            " next one.",
        ]
        message = notices.build_relay_alert(
            "Emails from senders who may not send WhatsApp updates", lines, self.settings, now=now
        )
        self._enqueue_note(_alert_key("not_allowed", int(now // DAY)), message)

    def _load_digest(self) -> dict[str, Any]:
        try:
            data = json.loads(self.store.get_meta(DIGEST_META) or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        more = data.get("more")
        return {
            "last_alert_at": _float(data.get("last_alert_at")),
            "pending": [entry for entry in pending if isinstance(entry, dict)][:DIGEST_LIMIT],
            "more": more if isinstance(more, int) and more > 0 else 0,
        }

    def _save_digest(self, data: dict[str, Any]) -> None:
        self.store.set_meta(DIGEST_META, json.dumps(data, separators=(",", ":")))

    def _dead_letter(self, item: QueueItem, error: str) -> None:
        if item.kind != KIND_RELAY:
            return
        now = self.clock()
        payload = item.payload if isinstance(item.payload, dict) else {}
        graph_id = str(payload.get("id") or "").strip()
        unreadable = False
        try:
            state = self.store.get_relay_state(item.key)
        except ValueError:  # an unreadable state: the administrators still have to hear about the item
            log.exception("the saved state of dead-lettered relay item #%s cannot be read", item.id)
            state, unreadable = None, True
        decision = (state or {}).get("decision")
        decision = decision if isinstance(decision, dict) else None
        steps_readable = state is not None and isinstance(state.get("steps", {}), dict)
        # Whether a replay could still send something, unless what follows is recorded.
        at_risk = (
            state is not None
            and decision is not None
            and steps_readable
            and (
                decision.get("action") == PENDING or _customer_steps_left(decision, state.get("steps") or {})
            )
        )
        if decision is not None and decision.get("action") == PENDING:
            # Given up while Meta's template list or the attachments were being looked at, after the sender
            # was verified: NOT sent and never sent later, and the employee hears so (owner decision 4).
            decision.update(
                action="refuse", refusal={"code": "not_finished", "text": GAVE_UP, "short": GAVE_UP_SHORT}
            )
        told = already_told = recorded = False
        command_item = decision is not None and decision.get("action") in COMMAND_ACTIONS and steps_readable
        if command_item:
            assert state is not None and decision is not None
            steps = state.setdefault("steps", {})
            _finish(decision, steps, now)
            # First of all: nothing more goes to the customer. Each write below is tried even if this fails.
            recorded = self._try_save(item.key, state)
            if (steps.get("notice") or {}).get("status") == SENT:
                already_told = True
            else:
                try:
                    # The notice step's own key: an outcome email that step queued before it was cut short
                    # is not sent a second time, and a replay finds this one (see _stop_if_told).
                    note_key = _result_key(graph_id or _safe_id(item.key))
                    added = self._enqueue_note(note_key, self._outcome(state))
                except Exception:
                    log.exception("could not tell the employee about dead-lettered relay item #%s", item.id)
                else:
                    told, already_told = added, not added
                    steps["notice"] = {"status": SENT, "at": now, "note": "dead"}
                    recorded = self._try_save(item.key, state) or recorded
        lines = [
            "The service gave up on a WhatsApp update email after repeated errors.",
            f"Error: {_first(error)}",
        ]
        employee = (decision or {}).get("employee")
        if isinstance(employee, dict) and employee.get("address"):
            lines.append(f"From: {employee['address']}")
        command = (decision or {}).get("command")
        command = command if isinstance(command, dict) else {}
        received = _float(command.get("received_at")) or _float((decision or {}).get("received_at"))
        received = received or _graph_time(payload.get("received"))
        if received:
            lines.append(f"Received: {_local_time(int(received), self.settings.mail_timezone)}")
        if unreadable or (
            decision is not None and decision.get("action") in COMMAND_ACTIONS and not command_item
        ):
            lines.append(
                "Its saved state cannot be read, so the service cannot tell what was sent. Do not replay it:"
                " check the chat in the WhatsApp Business app and tell the employee."
            )
        elif command_item:
            assert state is not None and decision is not None
            lines.extend(_summary(decision, state["steps"]))
            if told:
                lines.append("The employee has been told what did and did not go out.")
            elif already_told:
                lines.append("The employee had already been told the outcome.")
            else:
                lines.append("The employee could NOT be told (see the service log); please tell them.")
            if not at_risk or recorded or told or already_told:
                lines.append(
                    "Nothing more will be sent for this email. Replaying it (whatsapp-webhook replay-failed)"
                    " only finishes the bookkeeping, such as moving the email to its folder; it never sends"
                    " anything."
                )
            else:
                lines.append(
                    "The service could not record that nothing more may be sent for this email (a database"
                    " error, see the service log). Do NOT replay it (whatsapp-webhook replay-failed): a"
                    " replay could still send the update."
                )
        elif decision:
            lines.append("It was not an update command, so nothing was sent and nobody was told.")
        else:
            hours = f"{self.settings.relay_max_age_hours:g}"
            lines.append(
                "The email had not been read yet: nothing was sent, and its sender has not been told. After"
                " fixing the cause, replay it with whatsapp-webhook replay-failed (list-failed shows it). It"
                f" is then handled like a new email: sent if it is less than {hours} hours old, otherwise"
                " refused."
            )
        alert = notices.build_relay_alert(
            "A WhatsApp update email could not be finished", lines, self.settings, now=now
        )
        # One alert per time the worker gives up: the same for a repeated call, a new one after a replay.
        self._enqueue_note(_alert_key(f"dead:{_safe_id(item.key)}", int(item.first_attempt_at or now)), alert)
        log.error(
            "relay item #%s was dead-lettered; %s",
            item.id,
            "the employee was told" if told or already_told else "alerted",
        )

    # ------------------------------------------------------------------ small helpers
    def _too_old_text(self, received: float) -> str:
        hours = f"{self.settings.relay_max_age_hours:g}"
        when = _local_time(int(received), self.settings.mail_timezone)
        return (
            f"Your email arrived on {when}, more than {hours} hours ago. Updates are only sent within {hours}"
            " hours of arriving, because the information may be out of date by then."
        )

    def _counted(self, item: QueueItem, decision: dict[str, Any]) -> None:
        action = str(decision.get("action"))
        self._counts[action] = self._counts.get(action, 0) + 1
        self._last_decision_at = self.clock()
        employee = (decision.get("employee") or {}).get("address")
        if action == "gone":
            log.info("relay #%s: the email no longer exists; nothing to do", item.id)
        elif action == "ignore":
            log.info("relay #%s: not a command (%s); left in the Inbox", item.id, decision.get("why"))
        elif action == "not_allowed":
            log.warning(
                "relay #%s from %s: not accepted (%s); left in the Inbox",
                item.id,
                decision.get("from"),
                decision.get("why"),
            )
        elif action == "refuse":
            log.info("relay #%s from %s: refused (%s)", item.id, employee, decision["refusal"]["code"])
        else:
            routed = sum(1 for f in decision.get("files") or [] if f.get("route"))
            log.info(
                "relay #%s from %s: %s to %s, %d of %d file(s) to send",
                item.id,
                employee,
                "dry run" if action == "dry_run" else "sending",
                self._mask(decision.get("phone")),
                routed,
                len(decision.get("files") or []),
            )

    def _mask(self, phone: str | None) -> str:
        from app.worker import mask_phone  # here, not at the top: app.worker may import this module

        return mask_phone(phone, self.settings.log_mask_phones)


# ---------------------------------------------------------------------- pure helpers
def crm_match_text(result: LookupResult) -> str:
    """The "VShipCRM:" line of the employee's email: who the number belongs to, if anyone."""
    best = result.best
    if result.status in (LookupStatus.MATCHED, LookupStatus.NO_EMAIL) and best is not None:
        return best.record.label
    if result.status is LookupStatus.NO_MATCH:
        return "no CRM customer has this number"
    if result.status is LookupStatus.NO_PHONE:
        return "the number could not be looked up"
    if result.status is LookupStatus.DISABLED:
        return "CRM lookup is switched off"
    return f"CRM lookup failed: {result.reason or 'no reason given'}"


def _graph_id(item: QueueItem) -> str:
    value = item.payload.get("id") if isinstance(item.payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        from app.worker import PermanentFailure  # here, not at the top: app.worker may import this module

        raise PermanentFailure("the relay item names no email (no Graph message id)")
    return value.strip()


def _ignore(now: float, mail: MailInfo, why: str) -> dict[str, Any]:
    return {"action": "ignore", "decided_at": now, "from": mail.author, "why": why}


def _received_at(item: QueueItem, mail: MailInfo) -> float:
    """When the email arrived: Graph's receivedDateTime, else as the poller listed it, else when queued."""
    return mail.received_at or _graph_time(item.payload.get("received")) or float(item.received_at)


def _undecided(decision: Any) -> bool:
    """No decision yet, or only the pending record of one that was cut short (it is made again)."""
    return decision is None or (isinstance(decision, dict) and decision.get("action") == PENDING)


def _result_key(graph_id: str) -> str:
    """Queue key of the employee's outcome email (the notice step and the dead letter share it)."""
    return f"note:relay:{graph_id}:result"


def _customer_steps_left(decision: dict[str, Any], steps: dict[str, Any]) -> bool:
    """Whether a WhatsApp send of this decision has not been tried yet (only a ``send`` decision has any)."""
    if decision.get("action") != "send":
        return False
    if "template" not in steps:
        return True
    return any(f.get("route") and _file_step(f) not in steps for f in decision.get("files") or [])


def _never_repeated(step_id: str) -> bool:
    """Steps that reach the customer (or list an email in the digest) are never done a second time."""
    return step_id in ("template", "digest") or step_id.startswith("file:")


def _file_step(entry: dict[str, Any]) -> str:
    return f"file:{entry['id']}"


def _messages(state: dict[str, Any]) -> int:
    return sum(
        1
        for step_id, step in state["steps"].items()
        if (step_id == "template" or step_id.startswith("file:")) and step.get("status") == SENT
    )


def _spec_dict(spec: TemplateSpec) -> dict[str, Any]:
    return {"name": spec.name, "language": spec.language, "params": list(spec.params), "header": spec.header}


def _spec(data: dict[str, Any]) -> TemplateSpec:
    return TemplateSpec(data["name"], data["language"], tuple(data["params"]), data.get("header", "none"))


def _file_results(
    decision: dict[str, Any], steps: dict[str, Any], *, dry_run: bool = False
) -> list[notices.FileResult]:
    results: list[notices.FileResult] = []
    for entry in decision.get("files") or []:
        route = entry.get("route")
        if not route:
            results.append(notices.FileResult(entry["name"], False, entry.get("reason")))
            continue
        if dry_run:
            results.append(notices.FileResult(entry["name"], True, _HOW[route]))
            continue
        step = steps.get(_file_step(entry)) or {}
        if step.get("status") == SENT:
            results.append(notices.FileResult(entry["name"], True, _HOW[route]))
        elif step.get("status") == UNCERTAIN:
            results.append(notices.FileResult(entry["name"], None, step.get("error")))
        else:
            results.append(notices.FileResult(entry["name"], False, step.get("error")))
    return results


def _template_problems(state: dict[str, Any]) -> dict[str, str]:
    """Templates found not usable while handling this email, with Meta's reason (for the alert)."""
    decision, steps = state["decision"], state["steps"]
    problems: dict[str, str] = {}
    refusal = decision.get("refusal") or {}
    if decision.get("action") == "refuse" and refusal.get("code") == "template_not_ready":
        name = refusal.get("template") or (decision.get("template") or {}).get("name") or "?"
        problems[name] = refusal["text"]
    if decision.get("document_problem") and decision.get("document_template"):
        problems.setdefault(decision["document_template"]["name"], decision["document_problem"])
    for step in steps.values():
        if step.get("reason_code") == "template_not_ready" and step.get("template"):
            problems.setdefault(step["template"], step.get("problem") or step.get("error") or "")
    return problems


def _finish(decision: dict[str, Any], steps: dict[str, Any], now: float) -> None:
    """After a dead-letter: every customer step that has not happened will never happen."""
    for step_id, step in list(steps.items()):
        if step.get("status") == SENDING and _never_repeated(step_id):
            steps[step_id] = {"status": UNCERTAIN, "error": INTERRUPTED, "at": step.get("at")}
    if decision.get("action") != "send":
        return
    if "template" not in steps:
        steps["template"] = {"status": SKIPPED, "reason_code": "not_finished", "error": GAVE_UP, "at": now}
    template_sent = steps["template"].get("status") == SENT
    for entry in decision.get("files") or []:
        if entry.get("route") and _file_step(entry) not in steps:
            steps[_file_step(entry)] = {
                "status": SKIPPED,
                "reason_code": "not_finished",
                "error": GAVE_UP_FILE if template_sent else None,
                "at": now,
            }


def _summary(decision: dict[str, Any], steps: dict[str, Any]) -> list[str]:
    """What went out, in a few words, for the administrators."""
    if decision.get("action") == "refuse":
        code = decision["refusal"]["code"]
        if code == "not_finished":
            return ["The update: NOT sent (the service gave up while it was still checking the email)."]
        return [f"The update was refused ({code}); nothing was sent."]
    if decision.get("action") == "dry_run":
        return ["It was a dry run; nothing was sent."]
    words = {SENT: "sent", UNCERTAIN: "MAY NOT have been sent", REFUSED: "NOT sent", SKIPPED: "NOT sent"}
    template = (steps.get("template") or {}).get("status")
    lines = [f"The update: {words.get(template, 'NOT sent')}."]
    counts: dict[str, int] = {}
    for entry in decision.get("files") or []:
        status = (steps.get(_file_step(entry)) or {}).get("status") if entry.get("route") else SKIPPED
        label = words.get(status or SKIPPED, "NOT sent")
        counts[label] = counts.get(label, 0) + 1
    if counts:
        lines.append("Files: " + ", ".join(f"{count} {label}" for label, count in counts.items()) + ".")
    return lines


def _alert_key(kind: str, bucket: int) -> str:
    return f"note:relay_alert:{kind}:{bucket}"


def _due(last: float | None, now: float) -> bool:
    """Whether 24 hours have passed since ``last`` (a time in the future counts as passed)."""
    return last is None or not 0 <= now - last < DAY


def _safe_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _short_key(key: str) -> str:
    return _safe_id(key)[:12]


def _first(error: str) -> str:
    line = (str(error or "").strip().splitlines() or ["(no error text)"])[0]
    return line[:300]


def _graph_time(value: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
