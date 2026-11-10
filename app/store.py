"""SQLite-backed durable queue.

One table holds both inbound WhatsApp messages (``kind='message'``, keyed by the WhatsApp
message id so Meta's retries are de-duplicated) and alert emails (``kind='alert'``).
The worker claims rows, and on failure either reschedules them with backoff or marks them
``failed`` (the dead-letter list).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KIND_MESSAGE = "message"
KIND_ALERT = "alert"

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
        """Delete done items finished before ``older_than`` (failed items are kept for review)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM queue WHERE status = ? AND finished_at IS NOT NULL AND finished_at < ?",
                (STATUS_DONE, older_than),
            )
            return cur.rowcount

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
