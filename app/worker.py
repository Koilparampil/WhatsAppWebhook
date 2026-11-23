"""Background worker: drains the SQLite queue, builds and sends emails, retries, dead-letters.

Retry policy (configurable): exponential backoff starting at ``WORKER_BASE_DELAY_SECONDS``, capped
at ``WORKER_MAX_DELAY_SECONDS``, for at most ``WORKER_RETRY_WINDOW_SECONDS`` after the first attempt
(``ALERT_RETRY_WINDOW_SECONDS`` for alert emails). After that the item is marked ``failed`` (dead
letter) and, if enabled, an alert email is queued.

Robustness rules:

* The loop never dies. An error escaping an item (for example a SQLite I/O error while recording the
  outcome) is logged and counted, the loop pauses, and rows left in ``processing`` are re-queued.
* A graceful shutdown that interrupts an item re-queues it without counting a crash.
* An item found in ``processing`` at startup was interrupted by a crash; it is retried with backoff
  and dead-lettered after ``WORKER_MAX_CRASH_ATTEMPTS`` crashes, so one poisonous message cannot
  crash-loop the service.

"WhatsApp updates from email" (``app.relay``) puts three more kinds into the same queue: ``relay`` (an
employee's command email, handled by the relay processor), ``relay_status`` (WhatsApp reporting that a
message the feature sent could not be delivered) and ``note`` (a ready-made email of the feature: a notice
to the employee or an alert, retried as long as alerts are). A relay item may be retried and replayed like
any other: the processor records every step before and after it happens and never repeats a WhatsApp send.
WhatsApp asking an update to wait delays only that update, never the forwarding of customers' messages; so
does Graph throttling a relay mailbox other than the mailbox the emails are sent from (GRAPH_SENDER).
While the feature is switched off, relay items wait (looked at again every hour) instead of being given up,
even one found interrupted by crashes as often as WORKER_MAX_CRASH_ATTEMPTS allows.
Only items that send an email count as a successful send in the health status: a relay item sends none.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from app.config import Settings
from app.crm.directory import Directory, LookupResult, LookupStatus
from app.mail.compose import ComposeError, build_alert_email, build_part, plan_emails
from app.mail.transports import SendError, Transport
from app.relay.notices import from_payload
from app.store import (
    KIND_ALERT,
    KIND_MESSAGE,
    KIND_NOTE,
    KIND_RELAY,
    KIND_RELAY_STATUS,
    STATUS_PROCESSING,
    QueueItem,
    Store,
)
from app.whatsapp.media import DownloadedMedia, MediaDownloadError, download_media
from app.whatsapp.parser import InboundMessage, parse_webhook
from app.whatsapp.send import WhatsAppSendError

if TYPE_CHECKING:
    from app.relay.processor import RelayProcessor

log = logging.getLogger(__name__)

SPOOL_MARKER = ".whatsapp-webhook-spool"
OUTCOME_WRITE_DELAYS = (0.5, 1.0, 2.0)
RELAY_OFF_DELAY_SECONDS = 3600.0
RELAY_OFF_TEXT = "WhatsApp updates are switched off; waiting"
_RELAY_KINDS = (KIND_RELAY, KIND_RELAY_STATUS)
# Kinds that send an email when they are done. A relay item sends none itself (its outcome email is a note,
# and its emails_sent counts WhatsApp messages).
_EMAIL_KINDS = (KIND_MESSAGE, KIND_ALERT, KIND_NOTE)


class PermanentFailure(Exception):
    """The item can never succeed; dead-letter immediately."""


class RelayWaiting(Exception):
    """A relay item cannot be handled now (the feature is switched off); look at it again after ``delay``."""

    def __init__(self, reason: str, delay: float):
        super().__init__(reason)
        self.delay = delay


@dataclass(slots=True)
class ProcessOutcome:
    emails_sent: int
    lookup: LookupResult | None = None


def mask_phone(phone: str | None, enabled: bool = True) -> str:
    if not phone:
        return "unknown"
    if not enabled or len(phone) < 7:
        return phone
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


class Worker:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        directory: Directory,
        transport: Transport,
        http: httpx.AsyncClient,
        clock=time.time,
        *,
        relay: RelayProcessor | None = None,
    ):
        self.settings = settings
        self.store = store
        self.directory = directory
        self.transport = transport
        self.http = http
        self.clock = clock
        self.wakeup = asyncio.Event()
        self.last_send_ok_at: float | None = None
        self.last_error: str | None = None
        self.loop_errors = 0
        self.last_loop_error_at: float | None = None
        self.error_pause_seconds = 2.0  # doubled per consecutive loop error, capped at 60 s
        self.outcome_write_delays: tuple[float, ...] = OUTCOME_WRITE_DELAYS
        self.paused_until: float | None = None  # set when the mail service throttles us (Retry-After)
        self._current: str | None = None
        self._relay: RelayProcessor | None = None
        self._relay_since: float | None = None
        self.relay = relay

    @property
    def relay(self) -> RelayProcessor | None:
        """The "WhatsApp updates" processor; None while the feature is off (relay items then wait)."""
        return self._relay

    @relay.setter
    def relay(self, processor: RelayProcessor | None) -> None:
        self._relay = processor
        # A relay item that waited while the feature was off would otherwise find its retry window used
        # up and be given up at its first passing error; its window counts from now instead.
        self._relay_since = self.clock() if processor is not None else None

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        try:
            self.recover_interrupted()
            await asyncio.to_thread(self.clean_spool)
        except Exception:
            log.exception("startup recovery failed; continuing")
        consecutive = 0
        while not stop.is_set():
            try:
                if self.paused_until is not None and self.paused_until > self.clock():
                    # the mail service asked us to wait: send nothing at all until then
                    wait = self.paused_until - self.clock()
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=min(wait, self.settings.worker_poll_seconds)
                        )
                    except TimeoutError:
                        pass
                    continue
                self.paused_until = None
                item = self.store.claim_next(self.clock())
                if item is None:
                    await self._sleep_until_due(stop)
                    continue
                await self.handle(item)
                consecutive = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive += 1
                self.loop_errors += 1
                self.last_loop_error_at = self.clock()
                self.last_error = f"worker loop error: {exc!r}"
                log.exception("worker loop error (%d in a row); pausing before retrying", consecutive)
                pause = min(60.0, self.error_pause_seconds * 2 ** min(consecutive - 1, 5))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=pause)
                except TimeoutError:
                    pass
                self._requeue_stale_processing()
        log.info("worker stopped")

    async def _sleep_until_due(self, stop: asyncio.Event) -> None:
        due = self.store.next_due_at()
        timeout = self.settings.worker_poll_seconds
        if due is not None:
            timeout = max(0.05, min(timeout, due - self.clock()))
        self.wakeup.clear()
        stop_task = asyncio.ensure_future(stop.wait())
        wake_task = asyncio.ensure_future(self.wakeup.wait())
        try:
            await asyncio.wait({stop_task, wake_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()

    def _requeue_stale_processing(self) -> None:
        """After a loop error nothing is being processed, so any 'processing' row is stale."""
        try:
            for item in self.store.list_by_status(STATUS_PROCESSING, limit=1000):
                self.store.mark_retry(item.id, self.clock(), "re-queued after a worker error")
                log.warning("re-queued %s left in processing by a worker error", item.key)
        except Exception as exc:
            log.warning("could not re-queue stale items yet: %r", exc)

    # ------------------------------------------------------------------ startup recovery
    def _backoff(self, attempts: int) -> float:
        return min(
            self.settings.worker_base_delay_seconds * (2 ** max(0, attempts - 1)),
            self.settings.worker_max_delay_seconds,
        )

    def recover_interrupted(self) -> None:
        """Items left in ``processing`` by a crash get a backoff; repeat offenders are dead-lettered.

        Without the crash cap a message that kills the process (for example by exhausting memory)
        would be claimed again immediately after every restart, forever. Ordinary send failures and
        graceful shutdowns do not count as crashes.

        A relay item that reaches the cap while WhatsApp updates are switched off waits instead, like
        every relay item then: given up now, its sender would never hear what became of the update (only
        the relay processor can say), and a replay could still send it. The crash is not counted, so the
        item gets its last attempt once the feature is on again.
        """
        now = self.clock()
        for item in self.store.list_by_status(STATUS_PROCESSING, limit=10_000):
            crashes = item.crashes + 1
            reason = f"service stopped unexpectedly while processing (crash {crashes})"
            if crashes >= self.settings.worker_max_crash_attempts:
                if item.kind in _RELAY_KINDS and self.relay is None:
                    self.store.mark_retry(item.id, now + RELAY_OFF_DELAY_SECONDS, RELAY_OFF_TEXT)
                    log.warning(
                        "relay items waiting: feature is off (%s was interrupted %d time(s); not given up)",
                        item.key,
                        crashes,
                    )
                    continue
                self.store.mark_interrupted(item.id, now, reason)
                self._dead_letter(item, f"gave up after {crashes} interrupted attempt(s): {reason}")
                continue
            delay = self._backoff(item.attempts)
            self.store.mark_interrupted(item.id, now + delay, reason)
            log.warning("re-queued interrupted item %s in %.0fs (%s)", item.key, delay, reason)

    def clean_spool(self) -> int:
        """Remove partial downloads left behind by a crash.

        Only a directory carrying the service's marker file is cleaned, so a mis-pointed SPOOL_DIR
        can never delete anything else.
        """
        spool = Path(self.settings.spool_dir)
        marker = spool / SPOOL_MARKER
        if not spool.exists():
            spool.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return 0
        if not marker.exists():
            if any(spool.iterdir()):
                log.warning(
                    "SPOOL_DIR %s has no %s marker and is not empty; not cleaning it", spool, SPOOL_MARKER
                )
                return 0
            marker.touch()
            return 0
        removed = 0
        for child in spool.iterdir():
            if child.name == SPOOL_MARKER:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove stale spool entry %s: %s", child, exc)
        if removed:
            log.info("removed %d stale spool entr%s", removed, "y" if removed == 1 else "ies")
        return removed

    # ------------------------------------------------------------------ one item
    async def handle(self, item: QueueItem) -> None:
        self._current = item.key
        started = self.clock()
        try:
            try:
                if item.kind == KIND_MESSAGE:
                    outcome = await self.process_message(item)
                elif item.kind == KIND_ALERT:
                    outcome = await self.process_alert(item)
                elif item.kind == KIND_RELAY:
                    outcome = await self.process_relay(item)
                elif item.kind == KIND_RELAY_STATUS:
                    outcome = await self.process_relay_status(item)
                elif item.kind == KIND_NOTE:
                    outcome = await self.process_note(item)
                else:
                    raise PermanentFailure(f"unknown queue item kind {item.kind!r}")
                decision: tuple[str, object] = ("done", outcome)
            except asyncio.CancelledError:
                # graceful shutdown: hand the item back without counting it as a crash
                try:
                    self.store.mark_retry(item.id, self.clock(), "service stopped while processing")
                except Exception:
                    log.exception("could not re-queue %s during shutdown", item.key)
                raise
            except PermanentFailure as exc:
                decision = ("dead", f"permanent: {exc}")
            except RelayWaiting as exc:
                decision = ("wait", (str(exc), exc.delay))
            except WhatsAppSendError as exc:
                # WhatsApp asking for a pause holds up only this update: never the global pause, which
                # would stop the forwarding of customers' messages. An unclear outcome is never retried
                # (it is not retriable), so a customer cannot get the same message twice.
                if exc.retriable:
                    decision = ("retry", (str(exc), exc.retry_after))
                else:
                    decision = ("dead", f"not retriable: {exc}")
            except (MediaDownloadError, SendError) as exc:
                if getattr(exc, "retriable", True):
                    retry_after = getattr(exc, "retry_after", None)
                    if retry_after and self._pauses_sending(item):
                        self.paused_until = max(self.paused_until or 0.0, self.clock() + retry_after)
                        log.warning(
                            "mail service asked to wait %.0fs (Retry-After); pausing sends", retry_after
                        )
                    elif retry_after:
                        log.warning(
                            "the relay mailbox asked to wait %.0fs (Retry-After); only %s waits",
                            retry_after,
                            item.key,
                        )
                    decision = ("retry", (str(exc), retry_after))
                else:
                    decision = ("dead", f"not retriable: {exc}")
            except ComposeError as exc:
                decision = ("dead", f"compose: {exc}")
            except Exception as exc:  # unexpected bug: retry, then dead-letter with the traceback
                log.exception("unexpected error processing %s", item.key)
                decision = ("retry", f"{exc!r}\n{traceback.format_exc()[-1500:]}")
            await self._record(item, decision, started)
        finally:
            self._current = None

    async def _record(self, item: QueueItem, decision: tuple[str, object], started: float) -> None:
        """Persist the outcome, retrying briefly when SQLite is busy (e.g. a CLI holds the lock)."""
        kind, value = decision
        delays = (*self.outcome_write_delays, None)
        for delay in delays:
            try:
                if kind == "done":
                    assert isinstance(value, ProcessOutcome)
                    self.store.mark_done(item.id, value.emails_sent, self.clock())
                    if item.kind in _EMAIL_KINDS:
                        # /healthz: the last email that went out. A finished relay item sent none, so it
                        # must not make a failing mail service look healthy.
                        self.last_send_ok_at = self.clock()
                        self.last_error = None
                    log.info(
                        "processed %s kind=%s emails=%d in %.1fs",
                        item.key,
                        item.kind,
                        value.emails_sent,
                        self.clock() - started,
                    )
                elif kind == "retry":
                    if isinstance(value, tuple):
                        error, min_delay = value
                        self._schedule_retry(item, str(error), min_delay=min_delay)
                    else:
                        self._schedule_retry(item, str(value))
                elif kind == "wait":
                    # not an error and never given up for it: the item is simply looked at again later
                    assert isinstance(value, tuple)
                    reason, delay = value
                    self.store.mark_retry(item.id, self.clock() + float(delay), str(reason))
                    log.info(
                        "relay items waiting: feature is off (%s is looked at again in %.0f min)",
                        item.key,
                        float(delay) / 60,
                    )
                else:
                    self._dead_letter(item, str(value))
                return
            except sqlite3.OperationalError as exc:
                if delay is None:
                    raise
                log.warning(
                    "could not record the outcome of %s (%s); retrying in %.1fs", item.key, exc, delay
                )
                await asyncio.sleep(delay)

    def _pauses_sending(self, item: QueueItem) -> bool:
        """Whether Graph's Retry-After, met while handling ``item``, holds up every email the worker sends.

        Graph throttles an app per mailbox. Every other item meets it on the mailbox the emails are sent
        from, so nothing is sent until then. A relay item reads RELAY_MAILBOX instead: when that is not
        GRAPH_SENDER, its throttling does not reach the sending mailbox, and only that update waits (as
        long as asked), while customers' messages are still forwarded. Should Graph be unavailable for the
        sending mailbox as well, the next email meets that itself and pauses everything then.
        """
        if item.kind not in _RELAY_KINDS:
            return True
        return self.settings.relay_mailbox_address == self.settings.graph_sender.strip().lower()

    def _schedule_retry(self, item: QueueItem, error: str, min_delay: float | None = None) -> None:
        now = self.clock()
        first = item.first_attempt_at or now
        if item.kind in _RELAY_KINDS and self._relay_since is not None:
            first = max(first, self._relay_since)  # waiting while the feature was off used no window
        delay = max(self._backoff(item.attempts), min_delay or 0.0)
        next_at = now + delay
        window = (
            self.settings.alert_retry_window_seconds
            if item.kind in (KIND_ALERT, KIND_NOTE)
            else self.settings.worker_retry_window_seconds
        )
        if next_at - first > window:
            self._dead_letter(item, f"gave up after {item.attempts} attempt(s): {error}")
            return
        self.last_error = error
        self.store.mark_retry(item.id, next_at, error)
        log.warning(
            "retrying %s in %.0fs (attempt %d): %s", item.key, delay, item.attempts, error.splitlines()[0]
        )

    def _dead_letter(self, item: QueueItem, error: str) -> None:
        self.last_error = error
        if item.kind == KIND_RELAY:
            # The processor's emails first (the employee's outcome, the administrators' alert, the marks that
            # nothing more is sent), then the dead letter itself. A database that is locked right after the
            # item was marked failed would otherwise leave a given-up update unreported, and a replay could
            # still send it. In this order a locked database makes mark_failed raise, _record repeats this
            # whole step, and the processor queues nothing twice.
            self._relay_dead_letter(item, error)
        self.store.mark_failed(item.id, error, self.clock())
        log.error("dead-lettered %s: %s", item.key, error.splitlines()[0])
        if item.kind == KIND_MESSAGE and self.settings.alert_on_dead_letter:
            self.enqueue_alert(
                subject=f"WhatsApp message could not be forwarded ({item.key})",
                body=(
                    "A WhatsApp message could not be forwarded by email and was moved to the"
                    " dead-letter list.\n\n"
                    f"Queue key: {item.key}\nAttempts: {item.attempts}\nError: {error}\n\n"
                    "Replay it after fixing the cause with: whatsapp-webhook replay-failed "
                    f"{item.key}\n"
                ),
            )

    def _relay_dead_letter(self, item: QueueItem, error: str) -> None:
        """The employee and the administrators must learn what did and did not go out (the processor
        writes those emails). A failure to queue them must not stop the dead-lettering itself."""
        relay = self.relay
        if relay is None:
            log.warning(
                "dead-lettered %s while WhatsApp updates are switched off: its sender was not told", item.key
            )
            return
        try:
            relay.on_dead_letter(item, error)
        except Exception:
            log.exception("could not queue the notices about the given-up update %s", item.key)
        self.wakeup.set()

    def enqueue_alert(self, subject: str, body: str) -> None:
        key = f"alert:{uuid.uuid4()}"
        self.store.enqueue(KIND_ALERT, key, {"subject": subject, "body": body}, self.clock())
        self.wakeup.set()

    # ------------------------------------------------------------------ message pipeline
    async def process_message(self, item: QueueItem) -> ProcessOutcome:
        parsed = parse_webhook(item.payload.get("envelope"))
        messages = [m for m in parsed.messages if m.wamid == item.key]
        if not messages:
            raise PermanentFailure("stored payload no longer contains the message")
        msg: InboundMessage = messages[0]
        lookup = await self._safe_lookup(msg)
        log.info(
            "message %s from %s type=%s crm=%s",
            msg.wamid,
            mask_phone(msg.phone_e164, self.settings.log_mask_phones),
            msg.type,
            lookup.status.value,
        )
        media: DownloadedMedia | None = None
        spool = Path(self.settings.spool_dir)
        try:
            if msg.media is not None:
                media = await download_media(
                    self.http,
                    media=msg.media,
                    wamid=msg.wamid,
                    access_token=self.settings.wa_access_token,
                    graph_base_url=self.settings.wa_graph_base_url,
                    api_version=self.settings.wa_graph_api_version,
                    dest_dir=spool / _safe_dir(msg.wamid),
                    request_timeout=self.settings.wa_media_timeout_seconds,
                    app_secret=self.settings.wa_app_secret,
                )
            # Parts are built one at a time (base64 encoding is CPU and memory heavy) in a thread so
            # the webhook endpoint stays responsive. Parts delivered by an earlier attempt are
            # skipped, but only when the media was cut the same way (same plan signature).
            plan = await asyncio.to_thread(plan_emails, msg, lookup, media, self.settings)
            sent = min(item.emails_sent, plan.part_count)
            if sent and item.plan_sig != plan.signature:
                log.warning(
                    "split plan for %s changed (%s -> %s); re-sending every part",
                    msg.wamid,
                    item.plan_sig,
                    plan.signature,
                )
                sent = 0
            if sent:
                log.info("resuming %s at part %d/%d", msg.wamid, sent + 1, plan.part_count)
            for index in range(sent + 1, plan.part_count + 1):
                outgoing = await asyncio.to_thread(build_part, plan, index)
                await self.transport.send(outgoing.message)
                del outgoing
                sent += 1
                if plan.part_count > 1:
                    self.store.set_progress(item.id, sent, plan.signature)
                    log.info("sent part %d/%d for %s", index, plan.part_count, msg.wamid)
            return ProcessOutcome(emails_sent=sent, lookup=lookup)
        finally:
            if media is not None:
                media.cleanup()
            _rmdir_quiet(spool / _safe_dir(msg.wamid))

    async def _safe_lookup(self, msg: InboundMessage) -> LookupResult:
        """A bug in the CRM lookup must never block the email: degrade to a lookup error instead."""
        try:
            return await self.directory.lookup(msg.from_phone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("CRM lookup crashed for %s", msg.wamid)
            return LookupResult(
                LookupStatus.ERROR,
                reason=f"lookup crashed: {exc!r}",
                entities_searched=list(getattr(self.directory, "entities", [])),
                index_age_seconds=_index_age(self.directory),
            )

    async def process_alert(self, item: QueueItem) -> ProcessOutcome:
        subject = str(item.payload.get("subject") or "alert")
        body = str(item.payload.get("body") or "")
        await self.transport.send(build_alert_email(subject, body, self.settings))
        return ProcessOutcome(emails_sent=1)

    # ------------------------------------------------------------------ WhatsApp updates from email
    def _relay_or_wait(self) -> RelayProcessor:
        if self.relay is None:
            # Queued while the feature was on, reached while it is off: nothing is decided or sent now,
            # and the item is not given up either. It is handled once the feature is switched on again.
            raise RelayWaiting(RELAY_OFF_TEXT, RELAY_OFF_DELAY_SECONDS)
        return self.relay

    async def process_relay(self, item: QueueItem) -> ProcessOutcome:
        """An employee's command email; ``emails_sent`` counts the WhatsApp messages sent for it."""
        sent = await self._relay_or_wait().process(item)
        return ProcessOutcome(emails_sent=int(sent or 0))

    async def process_relay_status(self, item: QueueItem) -> ProcessOutcome:
        """WhatsApp reported a message of the feature as failed: the processor tells its sender (once)."""
        await self._relay_or_wait().process_status(item)
        return ProcessOutcome(emails_sent=0)

    async def process_note(self, item: QueueItem) -> ProcessOutcome:
        """A ready-made email of the feature (a notice to an employee, or an alert), sent as stored."""
        try:
            message = from_payload(item.payload)
        except ValueError as exc:
            raise PermanentFailure(f"the stored email cannot be rebuilt: {exc}") from exc
        await self.transport.send(message)
        return ProcessOutcome(emails_sent=1)

    # ------------------------------------------------------------------ maintenance
    def purge_old(self) -> int:
        cutoff = self.clock() - self.settings.store_retention_days * 86400
        return self.store.purge_finished(cutoff)

    def status(self) -> dict[str, object]:
        return {
            "current": self._current,
            "last_send_ok_at": self.last_send_ok_at,
            "last_error": self.last_error,
            "loop_errors": self.loop_errors,
            "last_loop_error_at": self.last_loop_error_at,
            "paused_until": self.paused_until,
        }


def _index_age(directory: object) -> float | None:
    getter = getattr(directory, "index_age", None)
    try:
        return getter() if callable(getter) else None
    except Exception:
        return None


def _safe_dir(wamid: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in wamid)[-80:] or "msg"


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
