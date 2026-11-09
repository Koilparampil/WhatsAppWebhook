"""Background worker: drains the SQLite queue, builds and sends emails, retries, dead-letters.

Retry policy (configurable): exponential backoff starting at ``WORKER_BASE_DELAY_SECONDS``, capped
at ``WORKER_MAX_DELAY_SECONDS``, for at most ``WORKER_RETRY_WINDOW_SECONDS`` after the first attempt.
After that the item is marked ``failed`` (dead letter) and, if enabled, an alert email is queued.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings
from app.crm.directory import Directory, LookupResult, LookupStatus
from app.mail.compose import ComposeError, build_alert_email, build_emails
from app.mail.transports import SendError, Transport
from app.store import KIND_ALERT, KIND_MESSAGE, QueueItem, Store
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
        self._current: str | None = None

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        recovered = self.store.recover_processing(self.clock())
        if recovered:
            log.info("re-queued %d item(s) left in processing state", recovered)
        while not stop.is_set():
            item = self.store.claim_next(self.clock())
            if item is None:
                await self._sleep_until_due(stop)
                continue
            await self.handle(item)
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
        if next_at - first > self.settings.worker_retry_window_seconds:
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
        lookup = await self.directory.lookup(msg.from_phone)
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
            emails = build_emails(msg, lookup, media, self.settings)
            sent = 0
            for outgoing in emails:
                await self.transport.send(outgoing.message)
                sent += 1
                if outgoing.part_count > 1:
                    log.info("sent part %d/%d for %s", outgoing.part_index, outgoing.part_count, msg.wamid)
            return ProcessOutcome(emails_sent=sent, lookup=lookup)
        finally:
            if media is not None:
                media.cleanup()
                _rmdir_quiet(spool / _safe_dir(msg.wamid))

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
            "lookup_statuses": [s.value for s in LookupStatus],
        }


def _safe_dir(wamid: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in wamid)[-80:] or "msg"


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
