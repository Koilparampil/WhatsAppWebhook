"""Background worker: drains the SQLite queue, builds and sends emails, retries, dead-letters.

Retry policy (configurable): exponential backoff starting at ``WORKER_BASE_DELAY_SECONDS``, capped
at ``WORKER_MAX_DELAY_SECONDS``, for at most ``WORKER_RETRY_WINDOW_SECONDS`` after the first attempt
(``ALERT_RETRY_WINDOW_SECONDS`` for alert emails). After that the item is marked ``failed`` (dead
letter) and, if enabled, an alert email is queued. The loop itself never dies: an unexpected error in
the loop body (for example a SQLite I/O error) is logged, counted, and retried after a pause.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings
from app.crm.directory import Directory, LookupResult, LookupStatus
from app.mail.compose import ComposeError, build_alert_email, build_part, plan_emails
from app.mail.transports import SendError, Transport
from app.store import KIND_ALERT, KIND_MESSAGE, STATUS_PROCESSING, QueueItem, Store
from app.whatsapp.media import DownloadedMedia, MediaDownloadError, download_media
from app.whatsapp.parser import InboundMessage, parse_webhook

log = logging.getLogger(__name__)


class PermanentFailure(Exception):
    """The item can never succeed; dead-letter immediately."""


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
        self._current: str | None = None

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

    # ------------------------------------------------------------------ startup recovery
    def recover_interrupted(self) -> None:
        """Items left in ``processing`` by a crash get a backoff; repeat offenders are dead-lettered.

        Without the attempt cap a message that kills the process (for example by exhausting memory)
        would be claimed again immediately after every restart, forever.
        """
        now = self.clock()
        for item in self.store.list_by_status(STATUS_PROCESSING, limit=10_000):
            reason = f"process died while processing (attempt {item.attempts})"
            window = (
                self.settings.alert_retry_window_seconds
                if item.kind == KIND_ALERT
                else self.settings.worker_retry_window_seconds
            )
            first = item.first_attempt_at or now
            if item.attempts >= self.settings.worker_max_crash_attempts or now - first > window:
                self._dead_letter(item, f"gave up after {item.attempts} interrupted attempt(s): {reason}")
                continue
            delay = min(
                self.settings.worker_base_delay_seconds * (2 ** max(0, item.attempts - 1)),
                self.settings.worker_max_delay_seconds,
            )
            self.store.mark_retry(item.id, now + delay, reason)
            log.warning("re-queued interrupted item %s in %.0fs (%s)", item.key, delay, reason)

    def clean_spool(self) -> int:
        """Remove partial downloads left behind by a crash. The spool only holds transient files."""
        spool = Path(self.settings.spool_dir)
        if not spool.exists():
            return 0
        removed = 0
        for child in spool.iterdir():
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
            if item.kind == KIND_MESSAGE:
                outcome = await self.process_message(item)
            elif item.kind == KIND_ALERT:
                outcome = await self.process_alert(item)
            else:
                raise PermanentFailure(f"unknown queue item kind {item.kind!r}")
        except PermanentFailure as exc:
            self._dead_letter(item, f"permanent: {exc}")
        except (MediaDownloadError, SendError) as exc:
            if getattr(exc, "retriable", True):
                self._schedule_retry(item, str(exc))
            else:
                self._dead_letter(item, f"not retriable: {exc}")
        except ComposeError as exc:
            self._dead_letter(item, f"compose: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # unexpected bug: retry, then dead-letter with the traceback
            log.exception("unexpected error processing %s", item.key)
            self._schedule_retry(item, f"{exc!r}\n{traceback.format_exc()[-1500:]}")
        else:
            self.store.mark_done(item.id, outcome.emails_sent, self.clock())
            self.last_send_ok_at = self.clock()
            self.last_error = None
            log.info(
                "processed %s kind=%s emails=%d in %.1fs",
                item.key,
                item.kind,
                outcome.emails_sent,
                self.clock() - started,
            )
        finally:
            self._current = None

    def _schedule_retry(self, item: QueueItem, error: str) -> None:
        now = self.clock()
        first = item.first_attempt_at or now
        delay = min(
            self.settings.worker_base_delay_seconds * (2 ** max(0, item.attempts - 1)),
            self.settings.worker_max_delay_seconds,
        )
        next_at = now + delay
        window = (
            self.settings.alert_retry_window_seconds
            if item.kind == KIND_ALERT
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
                )
            # Parts are built one at a time (base64 encoding is CPU and memory heavy) in a thread so
            # the webhook endpoint stays responsive; parts already delivered by an earlier attempt
            # are skipped because the chunk boundaries are deterministic.
            plan = await asyncio.to_thread(plan_emails, msg, lookup, media, self.settings)
            sent = min(item.emails_sent, plan.part_count)
            if sent:
                log.info("resuming %s at part %d/%d", msg.wamid, sent + 1, plan.part_count)
            for index in range(sent + 1, plan.part_count + 1):
                outgoing = await asyncio.to_thread(build_part, plan, index)
                await self.transport.send(outgoing.message)
                del outgoing
                sent += 1
                if plan.part_count > 1:
                    self.store.set_progress(item.id, sent)
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
