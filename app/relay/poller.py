"""Read the relay mailbox's Inbox every RELAY_POLL_SECONDS and queue the emails that look like commands.

The Inbox is the to-do list: an employee's ``WA | phone | booking`` email stays there until the worker has
handled it and moved it to RELAY_FOLDER. Each round lists what arrived since a cursor and queues every email
whose subject looks like a command, once, under its immutable Graph id (``relay:<id>``): the same email seen
again on a later round is a duplicate the queue ignores. Who sent it and what it says is decided later, by
the processor. Everything else in the Inbox is left exactly as it is: not queued, not moved, not marked as
read (owner decisions 5 and 6).

Where a round starts (meta ``relay_since``, ISO 8601 UTC):

* On the very first switch-on the cursor is set to now, and that moment is kept as well (meta
  ``relay_switched_on``): mail that was already in the Inbox, or that arrived before the feature was first
  switched on, is never handled (owner decision 14).
* Each round lists from 15 minutes before the cursor, but never from before the first switch-on: an email
  Exchange stamped a little before Graph lists it is not missed, and listing it twice is harmless. After a
  successful round the cursor moves to the time the round started; it never moves back. Switching the
  feature off and on again keeps the cursor, so what arrived in between is handled (or refused as too old).
* Mail older than the queue keeps its records (STORE_RETENTION_DAYS less an hour) is never listed: once the
  record of its handling is purged, an old command moved back into the Inbox would look new.

A round queues its emails oldest first, although Graph lists the Inbox newest first: the worker sends in
the order things were queued, and an update and the correction its sender wrote a few seconds later must
reach the customer in the order they were written.

A round that fails (Graph unreachable, access removed) is logged and tried again on the next round; the loop
never stops on an error. When the Inbox could not be read for 15 minutes in a row, the administrators
(ALERT_TO) get one alert, and one more when it can be read again. The outage they were told of is kept in
meta ``relay_inbox_alerted_since`` until that second alert is queued: a restart (often the very thing that
fixes the access) neither loses the second alert nor sends the first one again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.mail.transports import SendError
from app.relay.command import looks_like_command
from app.relay.mailbox import INBOX_PAGE, INBOX_PAGES, GraphMailbox
from app.relay.notices import build_relay_alert, to_payload
from app.store import KIND_NOTE, KIND_RELAY, Store

log = logging.getLogger(__name__)

CURSOR_KEY = "relay_since"  # where the next round starts (ISO 8601 UTC)
SWITCHED_ON_KEY = "relay_switched_on"  # when the feature was first switched on (ISO 8601 UTC); never changes
# start of the outage ALERT_TO was told of (ISO 8601 UTC); empty once the "can be read again" alert is queued
ALERTED_KEY = "relay_inbox_alerted_since"
OVERLAP_SECONDS = 15 * 60
ALERT_AFTER_SECONDS = 15 * 60
LIST_PAGES = INBOX_PAGES  # pages of INBOX_PAGE emails one round lists
# A full listing means more mail arrived since the cursor than one round lists (after an outage, or after
# the feature was off for a while). Graph lists newest first, so the oldest emails would never be seen: such
# a round lists once more, this many pages deep.
CATCH_UP_PAGES = 100
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def to_iso(timestamp: float) -> str:
    """Unix time as Graph's filter takes it (``2026-11-19T15:30:00Z``), cut to the second."""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime(_ISO_FORMAT)


def from_iso(value: object) -> float | None:
    """Unix time of an ISO 8601 date-time such as ``2026-11-19T15:30:00Z``; None if it cannot be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def oldest_first(listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The listed emails in the order they arrived; Graph lists the Inbox newest first.

    The worker takes queue items in the order they were queued, so this is the order in which customers get
    the updates. Emails stamped with the same second keep the reverse of Graph's order; one whose time
    cannot be read goes last.
    """

    def arrival(message: dict[str, Any]) -> float:
        stamp = from_iso(message.get("receivedDateTime"))
        return math.inf if stamp is None else stamp

    return sorted(reversed(listed), key=arrival)


class RelayPoller:
    """Queues the command-shaped emails of the relay mailbox's Inbox (see the module docstring)."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        mailbox: GraphMailbox,
        clock: Callable[[], float] = time.time,
        wakeup: asyncio.Event | None = None,
        on_round: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.mailbox = mailbox
        self.clock = clock
        self.wakeup = wakeup  # the worker's: set after anything was queued
        self.on_round = on_round  # called after every round (the processor's not-allowed digest)
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.failing_since: float | None = None
        self.queued = 0
        self._cursor: str | None = None  # as stored, for the health status
        # Start of the outage ALERT_TO was told of; kept in the store, as the "can be read again" alert is
        # owed even when the service restarts in between.
        self._alerted_since: float | None = self._load_alerted()

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set. Never ends on an error; a round in progress is dropped on stop."""
        log.info(
            "relay poller started for %s (every %.0f s)",
            self.mailbox.mailbox,
            self.settings.relay_poll_seconds,
        )
        try:
            self.ensure_cursor()
        except Exception:
            log.exception("could not set up the relay Inbox cursor yet; the first round tries again")
        while not stop.is_set():
            delay = float(self.settings.relay_poll_seconds)
            try:
                finished, delay = await self._round_unless_stopped(stop)
                if not finished:
                    break
                self._call_on_round()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep polling whatever happens: the next round may work
                log.exception("relay poller error; trying again in %.0f s", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        log.info("relay poller stopped")

    async def _round_unless_stopped(self, stop: asyncio.Event) -> tuple[bool, float]:
        """One round, given up as soon as ``stop`` is set: (whether it finished, delay before the next)."""
        round_task = asyncio.ensure_future(self._quiet_round())
        stop_task = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({round_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            if not round_task.done():
                # A round writes nothing before Graph's listing has come back, so one still waiting for
                # it can simply be dropped.
                round_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await round_task
        if round_task.cancelled():
            # dropped because of the stop, or cancelled from within (then it simply counts as a round)
            return not stop.is_set(), float(self.settings.relay_poll_seconds)
        return True, round_task.result()

    async def _quiet_round(self) -> float:
        """poll_once without raising (it records its own errors); returns the delay before the next round."""
        delay = float(self.settings.relay_poll_seconds)
        try:
            await self.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_after = exc.retry_after if isinstance(exc, SendError) else None
            if retry_after:
                delay = max(delay, float(retry_after))  # Graph asked us to slow down
        return delay

    def _call_on_round(self) -> None:
        if self.on_round is None:
            return
        try:
            self.on_round()
        except Exception:
            log.exception("the relay's per-round check (not-allowed sender digest) failed")

    # ------------------------------------------------------------------ one round
    def ensure_cursor(self) -> None:
        """On the very first switch-on, start the cursor (and remember the switch-on) now."""
        self._positions(self.clock())

    async def poll_once(self) -> int:
        """List the Inbox once and queue the command-shaped emails not queued yet. Returns how many.

        An error is recorded for the health status (and the outage alert) and raised again.
        """
        started = self.clock()
        try:
            added = await self._round(started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(exc)
            raise
        self._succeeded()
        return added

    async def _round(self, started: float) -> int:
        cursor, switched_on = self._positions(started)
        since = max(min(cursor, started) - OVERLAP_SECONDS, switched_on)
        keep = self.settings.store_retention_days * 86400 - 3600
        if keep > 0:
            since = max(since, started - keep)
        since_iso = to_iso(since)
        listed = await self.mailbox.list_inbox(since_iso, pages=LIST_PAGES)
        if len(listed) >= LIST_PAGES * INBOX_PAGE:
            log.warning(
                "the relay Inbox listing was full (%d emails since %s); listing up to %d pages once",
                len(listed),
                since_iso,
                CATCH_UP_PAGES,
            )
            listed = await self.mailbox.list_inbox(since_iso, pages=CATCH_UP_PAGES)
            if len(listed) >= CATCH_UP_PAGES * INBOX_PAGE:
                log.error(
                    "more than %d emails reached %s since %s: older command emails among them were not seen",
                    len(listed),
                    self.mailbox.mailbox,
                    since_iso,
                )
        added = 0
        # Oldest first: queued in Graph's order (newest first), an update and the correction written right
        # after it would reach the customer the wrong way round.
        for message in oldest_first(listed):
            if not looks_like_command(str(message.get("subject") or "")):
                continue  # not a command: left in the Inbox untouched
            received = message.get("receivedDateTime")
            stamp = from_iso(received)
            if stamp is not None and stamp < switched_on:
                continue  # arrived before the feature was first switched on
            message_id = str(message["id"])
            payload = {"id": message_id, "received": received}
            if self.store.enqueue(KIND_RELAY, f"relay:{message_id}", payload, self.clock()):
                added += 1
        new_cursor = to_iso(max(cursor, started))
        self.store.set_meta(CURSOR_KEY, new_cursor)
        self._cursor = new_cursor
        if added:
            self.queued += added
            log.info("relay mailbox %s: %d new command email(s) queued", self.mailbox.mailbox, added)
            self._wake()
        return added

    def _positions(self, now: float) -> tuple[float, float]:
        """(cursor, first switch-on) as Unix times; on the very first switch-on both start ``now``."""
        cursor_text = self.store.get_meta(CURSOR_KEY)
        switched_text = self.store.get_meta(SWITCHED_ON_KEY)
        if cursor_text is None:
            start = to_iso(now)
            if switched_text is None:
                # written first: should the cursor not be written, the switch-on moment is kept anyway
                self.store.set_meta(SWITCHED_ON_KEY, start)
                switched_text = start
                log.info(
                    "WhatsApp updates switched on: command emails reaching %s from now on are handled",
                    self.mailbox.mailbox,
                )
            else:
                log.warning("the relay Inbox cursor %s was missing; it starts again now", CURSOR_KEY)
            self.store.set_meta(CURSOR_KEY, start)
            cursor_text = start
        cursor, switched_on = from_iso(cursor_text), from_iso(switched_text)
        if cursor is None and switched_on is None:
            log.warning("the relay Inbox cursor %r cannot be read; it starts again now", cursor_text)
            start = to_iso(now)
            self.store.set_meta(SWITCHED_ON_KEY, start)
            self.store.set_meta(CURSOR_KEY, start)
            cursor = switched_on = from_iso(start)
        elif switched_on is None:
            # unknown: the cursor is the earliest moment known to be after the first switch-on
            self.store.set_meta(SWITCHED_ON_KEY, str(cursor_text))
            switched_on = cursor
        elif cursor is None:
            # Look again from the first switch-on (within the store's retention): every email already
            # queued is a duplicate the queue ignores, and none is skipped.
            log.warning(
                "the relay Inbox cursor %r cannot be read; looking again from %s", cursor_text, switched_text
            )
            cursor = switched_on
        assert cursor is not None and switched_on is not None
        self._cursor = to_iso(cursor)
        return cursor, switched_on

    # ------------------------------------------------------------------ health and the outage alert
    def _failed(self, exc: Exception) -> None:
        error = str(exc) or repr(exc)
        now = self.clock()
        if error != self.last_error:  # once per distinct error, not every round
            if isinstance(exc, SendError):
                log.warning("could not read the relay mailbox %s: %s", self.mailbox.mailbox, error)
            else:
                log.error("relay Inbox round failed: %r", exc, exc_info=exc)
        self.last_error = error
        if self.failing_since is None:
            self.failing_since = now
        if self._alerted_since is None and now - self.failing_since >= ALERT_AFTER_SECONDS:
            if self._queue_alert(
                f"note:relay:inbox-down:{int(self.failing_since)}", *self._down_alert(now), now=now
            ):
                self._alerted_since = self.failing_since
                self._remember_alerted(self.failing_since)

    def _succeeded(self) -> None:
        now = self.clock()
        if self._alerted_since is not None:
            if self._queue_alert(
                f"note:relay:inbox-up:{int(self._alerted_since)}", *self._up_alert(now), now=now
            ):
                self._alerted_since = None
                self._remember_alerted(None)
        if self.failing_since is not None:
            log.info("the relay mailbox %s can be read again", self.mailbox.mailbox)
        self.failing_since = None
        self.last_ok_at = now
        self.last_error = None

    def _down_alert(self, now: float) -> tuple[str, list[str]]:
        address = self.mailbox.mailbox
        minutes = int((now - (self.failing_since or now)) // 60)
        hours = f"{self.settings.relay_max_age_hours:g}"
        return (
            f"WhatsApp updates: the Inbox of {address} cannot be read",
            [
                f"The service has not been able to read the Inbox of {address} for {minutes} minutes.",
                "Employees' WhatsApp update emails are waiting there unseen: nothing is sent to customers,"
                " and the employees get no confirmation, until the Inbox can be read again. Customers'"
                " WhatsApp messages are still forwarded by email as usual.",
                f"Last error: {self.last_error}",
                "What to do: run `whatsapp-webhook relay-check` on the server; most often the app has lost"
                " its access to the mailbox (docs/whatsapp-updates-setup.md). You get another email when the"
                f" Inbox can be read again. The waiting emails are handled then; those more than {hours}"
                " hours old by then are refused, with a notice to their senders.",
            ],
        )

    def _up_alert(self, now: float) -> tuple[str, list[str]]:
        address = self.mailbox.mailbox
        minutes = int((now - (self._alerted_since or now)) // 60)
        return (
            f"WhatsApp updates: the Inbox of {address} can be read again",
            [
                f"The service can read the Inbox of {address} again (it could not for {minutes} minutes).",
                "The WhatsApp update emails that were waiting are being handled now.",
            ],
        )

    def _load_alerted(self) -> float | None:
        """The outage ALERT_TO was told of before this process started (None: none is open)."""
        try:
            return from_iso(self.store.get_meta(ALERTED_KEY))
        except Exception:
            log.exception("could not read whether ALERT_TO was told that the relay Inbox cannot be read")
            return None

    def _remember_alerted(self, since: float | None) -> None:
        """Keep the told-of outage across restarts. Should this fail, the alerts' fixed queue keys still
        keep either of them from being queued twice; only a restart could then lose the second one."""
        try:
            self.store.set_meta(ALERTED_KEY, "" if since is None else to_iso(since))
        except Exception:
            log.exception("could not record the state of the relay Inbox alerts")

    def _queue_alert(self, key: str, subject: str, lines: list[str], *, now: float) -> bool:
        """Queue an alert to ALERT_TO (a KIND_NOTE item, sent by the worker); False if it could not be."""
        try:
            message = build_relay_alert(subject, lines, self.settings, now=now)
            self.store.enqueue(KIND_NOTE, key, to_payload(message), now)
        except Exception:
            log.exception("could not queue the alert %r; trying again next round", subject)
            return False
        log.warning("alert queued for ALERT_TO: %s", subject)
        self._wake()
        return True

    def _wake(self) -> None:
        if self.wakeup is not None:
            self.wakeup.set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "mailbox": self.mailbox.mailbox,
            "cursor": self._cursor,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
            "failing_since": self.failing_since,
            "queued": self.queued,
        }
