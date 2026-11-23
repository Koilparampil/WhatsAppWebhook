"""SQLite-backed durable queue.

One table holds both inbound WhatsApp messages (``kind='message'``, keyed by the WhatsApp
message id so Meta's retries are de-duplicated) and alert emails (``kind='alert'``).
The worker claims rows, and on failure either reschedules them with backoff or marks them
``failed`` (the dead-letter list).

The "WhatsApp updates from email" feature (``app.relay``) adds three queue kinds and two tables:

* ``kind='relay'`` (key ``relay:<immutable Graph message id>``): an employee's command email, so the
  same email is handled once only; ``kind='relay_status'`` (key ``relay_status:<wamid>``): a "failed"
  delivery report for a message the feature sent; ``kind='note'`` (key ``note:<anything unique>``): a
  ready-made email the feature sends (outcome notices to the employee, alerts).
* ``relay_state`` holds the step state of each relay item (written before and after every side effect),
  so a retry, a crash or ``replay-failed`` never sends a WhatsApp message twice.
* ``relay_sent`` records every WhatsApp message id the feature sent, so a later "failed" status can be
  reported to the employee who sent the command (once).

Meta keys used by the feature: ``relay_since`` (ISO 8601 UTC cursor of the Inbox poll),
``relay_switched_on`` (when the feature was first switched on, ISO 8601 UTC; never changes),
``relay_inbox_alerted_since`` (start of the Inbox outage ALERT_TO was told of, ISO 8601 UTC; empty once the
"can be read again" alert is queued), ``relay_not_allowed`` (JSON state of the not-allowed-sender digest) and
``relay_template_alert:<name>`` (time of the last "template not ready" alert).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.whatsapp.parser import parse_webhook

KIND_MESSAGE = "message"
KIND_ALERT = "alert"
KIND_RELAY = "relay"  # key relay:<immutable Graph message id>
KIND_RELAY_STATUS = "relay_status"  # key relay_status:<wamid>
KIND_NOTE = "note"  # key note:<anything unique>

# WhatsApp writes these itself (e.g. "the customer changed their number"); the customer did not write to
# the business, so they do not open the 24-hour customer service window.
_NOT_FROM_CUSTOMER_TYPES = frozenset({"system"})

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    first_attempt_at REAL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    finished_at REAL,
    emails_sent INTEGER NOT NULL DEFAULT 0,
    crashes INTEGER NOT NULL DEFAULT 0,
    plan_sig TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status_next ON queue(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS relay_state (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_sent (
    wamid TEXT PRIMARY KEY,
    item_key TEXT NOT NULL,
    what TEXT NOT NULL,
    created_at REAL NOT NULL,
    failure_notified INTEGER NOT NULL DEFAULT 0
);
"""

# columns added after the first release; created on open when an older database lacks them
_MIGRATIONS = {
    "crashes": "ALTER TABLE queue ADD COLUMN crashes INTEGER NOT NULL DEFAULT 0",
    "plan_sig": "ALTER TABLE queue ADD COLUMN plan_sig TEXT",
}


@dataclass(slots=True)
class QueueItem:
    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    status: str
    attempts: int
    received_at: float
    first_attempt_at: float | None
    next_attempt_at: float
    last_error: str | None
    finished_at: float | None
    emails_sent: int
    crashes: int = 0
    plan_sig: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> QueueItem:
        keys = row.keys()
        return cls(
            id=row["id"],
            kind=row["kind"],
            key=row["key"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            attempts=row["attempts"],
            received_at=row["received_at"],
            first_attempt_at=row["first_attempt_at"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            finished_at=row["finished_at"],
            emails_sent=row["emails_sent"],
            crashes=row["crashes"] if "crashes" in keys else 0,
            plan_sig=row["plan_sig"] if "plan_sig" in keys else None,
        )


class Store:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(queue)")}
            for column, ddl in _MIGRATIONS.items():
                if column not in existing:
                    self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ enqueue / claim
    def enqueue(self, kind: str, key: str, payload: dict[str, Any], now: float | None = None) -> bool:
        """Insert a new item. Returns False when ``key`` already exists (duplicate delivery)."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO queue (kind, key, payload, status, received_at, next_attempt_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (kind, key, json.dumps(payload, separators=(",", ":")), STATUS_PENDING, now, now),
            )
            return cur.rowcount == 1

    def claim_next(self, now: float | None = None) -> QueueItem | None:
        """Atomically pick the oldest due pending item and mark it processing."""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM queue WHERE status = ? AND next_attempt_at <= ?"
                    " ORDER BY next_attempt_at ASC, id ASC LIMIT 1",
                    (STATUS_PENDING, now),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE queue SET status = ?, attempts = attempts + 1,"
                    " first_attempt_at = COALESCE(first_attempt_at, ?) WHERE id = ?",
                    (STATUS_PROCESSING, now, row["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self.get_by_id(row["id"])

    def next_due_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(next_attempt_at) AS due FROM queue WHERE status = ?", (STATUS_PENDING,)
            ).fetchone()
            return row["due"] if row and row["due"] is not None else None

    # ------------------------------------------------------------------ outcomes
    def mark_done(self, item_id: int, emails_sent: int = 0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status = ?, finished_at = ?, emails_sent = ?, last_error = NULL"
                " WHERE id = ?",
                (STATUS_DONE, now, emails_sent, item_id),
            )

    def set_progress(self, item_id: int, emails_sent: int, plan_sig: str | None = None) -> None:
        """Record how many emails (parts) of an item have been sent, and how the media was cut."""
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET emails_sent = ?, plan_sig = ? WHERE id = ?",
                (emails_sent, plan_sig, item_id),
            )

    def mark_interrupted(self, item_id: int, next_attempt_at: float, error: str) -> int:
        """An item found mid-processing after a crash: count the crash and schedule a retry."""
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status = ?, next_attempt_at = ?, last_error = ?, crashes = crashes + 1"
                " WHERE id = ?",
                (STATUS_PENDING, next_attempt_at, error[:4000], item_id),
            )
            row = self._conn.execute("SELECT crashes FROM queue WHERE id = ?", (item_id,)).fetchone()
            return int(row["crashes"]) if row else 0

    def mark_retry(self, item_id: int, next_attempt_at: float, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                (STATUS_PENDING, next_attempt_at, error[:4000], item_id),
            )

    def mark_failed(self, item_id: int, error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status = ?, finished_at = ?, last_error = ? WHERE id = ?",
                (STATUS_FAILED, now, error[:4000], item_id),
            )

    def recover_processing(self, now: float | None = None) -> int:
        """After a crash, items left in ``processing`` become pending again."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status = ?, next_attempt_at = ? WHERE status = ?",
                (STATUS_PENDING, now, STATUS_PROCESSING),
            )
            return cur.rowcount

    # ------------------------------------------------------------------ queries
    def get(self, key: str) -> QueueItem | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue WHERE key = ?", (key,)).fetchone()
            return QueueItem.from_row(row) if row else None

    def get_by_id(self, item_id: int) -> QueueItem | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
            return QueueItem.from_row(row) if row else None

    def list_by_status(self, status: str, limit: int = 100) -> list[QueueItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
            return [QueueItem.from_row(r) for r in rows]

    def requeue(self, key: str | None = None, now: float | None = None) -> int:
        """Move failed items back to pending (one key, or all when ``key`` is None)."""
        now = time.time() if now is None else now
        with self._lock:
            if key is None:
                cur = self._conn.execute(
                    "UPDATE queue SET status = ?, next_attempt_at = ?, first_attempt_at = NULL, attempts = 0,"
                    " emails_sent = 0, crashes = 0, plan_sig = NULL, finished_at = NULL WHERE status = ?",
                    (STATUS_PENDING, now, STATUS_FAILED),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE queue SET status = ?, next_attempt_at = ?, first_attempt_at = NULL, attempts = 0,"
                    " emails_sent = 0, crashes = 0, plan_sig = NULL, finished_at = NULL"
                    " WHERE key = ? AND status = ?",
                    (STATUS_PENDING, now, key, STATUS_FAILED),
                )
            return cur.rowcount

    def purge_finished(self, older_than: float) -> int:
        """Delete done items finished before ``older_than`` (failed items are kept for review).

        Also deletes the relay step state of items that are no longer in the queue (a failed item keeps its
        state, so ``replay-failed`` stays safe) and the records of WhatsApp messages the relay sent before
        ``older_than``. Returns the number of queue items deleted, as before.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "DELETE FROM queue WHERE status = ? AND finished_at IS NOT NULL AND finished_at < ?",
                    (STATUS_DONE, older_than),
                )
                purged = cur.rowcount
                self._conn.execute("DELETE FROM relay_state WHERE key NOT IN (SELECT key FROM queue)")
                self._conn.execute("DELETE FROM relay_sent WHERE created_at < ?", (older_than,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return purged

    def stats(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            counts = {
                row["status"]: row["n"]
                for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM queue GROUP BY status")
            }
            oldest = self._conn.execute(
                "SELECT MIN(received_at) AS t FROM queue WHERE status IN (?, ?)",
                (STATUS_PENDING, STATUS_PROCESSING),
            ).fetchone()["t"]
        return {
            "pending": counts.get(STATUS_PENDING, 0),
            "processing": counts.get(STATUS_PROCESSING, 0),
            "done": counts.get(STATUS_DONE, 0),
            "failed": counts.get(STATUS_FAILED, 0),
            "oldest_unfinished_age_seconds": (now - oldest) if oldest else 0.0,
        }

    # ------------------------------------------------------------------ key/value meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            return row["v"] if row else None

    # ------------------------------------------------------------------ WhatsApp updates from email (relay)
    def get_relay_state(self, key: str) -> dict[str, Any] | None:
        """The step state saved for the relay queue item ``key``, or None when none was saved yet.

        A saved state that cannot be read back raises instead of returning None: None would make the
        processor start the item from scratch and possibly send the WhatsApp message a second time.
        """
        with self._lock:
            row = self._conn.execute("SELECT state FROM relay_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        state = json.loads(row["state"])
        if not isinstance(state, dict):
            raise ValueError(f"the saved relay state of {key} is not a JSON object")
        return state

    def save_relay_state(self, key: str, state: dict[str, Any], now: float | None = None) -> None:
        """Replace the step state of the relay queue item ``key`` (the processor calls this before and after
        every side effect, so a re-run knows exactly what already happened)."""
        if not isinstance(state, dict):
            raise TypeError("the relay state must be a dict")
        now = time.time() if now is None else now
        data = json.dumps(state, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO relay_state (key, state, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (key, data, now),
            )

    def record_relay_sent(self, wamid: str, item_key: str, what: str, now: float | None = None) -> bool:
        """Remember a WhatsApp message the relay sent, so a later "failed" status can be reported.

        ``item_key`` is the relay queue item that sent it, ``what`` says what it was ("update" or
        "file:<name>"). Returns False when ``wamid`` was recorded before; the first record, and whether its
        failure was already reported, is kept.
        """
        if not wamid:
            raise ValueError("a sent WhatsApp message needs its message id (wamid)")
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO relay_sent (wamid, item_key, what, created_at) VALUES (?, ?, ?, ?)",
                (wamid, item_key, what, now),
            )
            return cur.rowcount == 1

    def get_relay_sent(self, wamid: str) -> dict[str, Any] | None:
        """The record of a WhatsApp message the relay sent: wamid, item_key, what, created_at and
        failure_notified (bool); None when this wamid was not sent by the relay (or was purged)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM relay_sent WHERE wamid = ?", (wamid,)).fetchone()
        if row is None:
            return None
        return {
            "wamid": row["wamid"],
            "item_key": row["item_key"],
            "what": row["what"],
            "created_at": row["created_at"],
            "failure_notified": bool(row["failure_notified"]),
        }

    def mark_relay_failure_notified(self, wamid: str) -> bool:
        """Note that the employee was told ``wamid`` could not be delivered.

        True only the first time, so Meta sending the "failed" status more than once still produces one
        email; False when it was already marked or the wamid is not a relay message.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE relay_sent SET failure_notified = 1 WHERE wamid = ? AND failure_notified = 0",
                (wamid,),
            )
            return cur.rowcount == 1

    def recent_inbound_timestamp(
        self, phone_digits: str, since: float, *, phone_number_id: str | None = None
    ) -> int | None:
        """Newest WhatsApp timestamp (Unix seconds) of a message the customer ``phone_digits`` sent us.

        Tells whether the customer's 24-hour customer service window is open (WhatsApp accepts free-form
        messages, such as files, only then). Looks at the inbound messages queued at or after ``since``
        (``received_at``, any status) and returns the message's own ``timestamp`` - when the customer sent
        it, which can be much older than when the webhook reached us - so the caller decides with it.
        ``phone_digits`` may carry a leading "+"; only its digits are compared with the sender's number.
        With ``phone_number_id``, only messages sent to that business number count (a window is per
        business number). WhatsApp's own notices (type ``system``) never count. None: no such message.
        """
        digits = "".join(ch for ch in phone_digits or "" if ch.isdigit())
        if not digits:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, payload FROM queue WHERE kind = ? AND received_at >= ?",
                (KIND_MESSAGE, since),
            ).fetchall()
        newest: int | None = None
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                envelope = payload.get("envelope") if isinstance(payload, dict) else None
                messages = parse_webhook(envelope, phone_number_id).messages
            except (TypeError, ValueError):
                continue  # an unreadable row is no evidence of a customer message
            for message in messages:
                if message.wamid != row["key"] or message.type in _NOT_FROM_CUSTOMER_TYPES:
                    continue
                sender = "".join(ch for ch in message.from_phone or "" if ch.isdigit())
                if sender != digits or message.timestamp <= 0:
                    continue
                if newest is None or message.timestamp > newest:
                    newest = message.timestamp
        return newest
