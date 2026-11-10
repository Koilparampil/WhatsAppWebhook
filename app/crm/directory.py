"""In-memory phone → CRM record index with periodic and on-miss refresh.

VShipCRM exposes no phone search, so the configured list endpoints (Customer by default) are
downloaded in full, indexed by E.164 number and by last-ten-digit suffix, and consulted locally.
A stale index keeps serving when the CRM is unreachable; the age is reported in every result.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.crm.client import CrmError, VShipClient
from app.crm.phones import CrmRecord, MatchCandidate, rank_candidates, record_from_api, whatsapp_phone_key

log = logging.getLogger(__name__)


class LookupStatus(StrEnum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    NO_EMAIL = "no_email"  # a customer matched but has no email address on record
    NO_PHONE = "no_phone"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(slots=True)
class LookupResult:
    status: LookupStatus
    candidates: list[MatchCandidate] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    reason: str | None = None
    index_age_seconds: float | None = None
    entities_searched: list[str] = field(default_factory=list)
    lookup_key: str | None = None

    @property
    def best(self) -> MatchCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def matched(self) -> bool:
        return self.status is LookupStatus.MATCHED


@dataclass(slots=True)
class _EntityIndex:
    entity: str
    records: list[CrmRecord]
    fetched_at: float
    raw_count: int


class Directory:
    def __init__(
        self,
        client: VShipClient | None,
        *,
        entities: list[str],
        default_region: str,
        include_inactive: bool = False,
        reply_to_all: bool = True,
        refresh_seconds: int = 900,
        miss_refresh_min_age_seconds: int = 60,
        clock=time.time,
    ):
        self._client = client
        self.entities = list(entities)
        self._region = default_region
        self._include_inactive = include_inactive
        self._reply_to_all = reply_to_all
        self._refresh_seconds = refresh_seconds
        self._miss_min_age = miss_refresh_min_age_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._by_entity: dict[str, _EntityIndex] = {}
        self._by_e164: dict[str, list[MatchCandidate]] = {}
        self._by_suffix: dict[str, list[MatchCandidate]] = {}
        self.last_refresh_at: float | None = None
        self.last_attempt_at: float | None = None
        self.last_refresh_failed = False
        self.last_error: str | None = None
        self._attempts_completed = 0

    # ------------------------------------------------------------------ status
    @property
    def enabled(self) -> bool:
        return self._client is not None and bool(self.entities)

    def index_age(self) -> float | None:
        return (self._clock() - self.last_refresh_at) if self.last_refresh_at is not None else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "entities": self.entities,
            "indexed_records": {e: len(ix.records) for e, ix in self._by_entity.items()},
            "indexed_numbers": len(self._by_e164),
            "index_age_seconds": self.index_age(),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ refresh
    async def refresh(self) -> dict[str, int]:
        """Reload every entity. Entities that fail keep their previous data; raises only if all fail."""
        if not self.enabled:
            return {}
        assert self._client is not None
        attempts_before = self._attempts_completed
        async with self._lock:
            if self._attempts_completed != attempts_before:
                # another caller finished an attempt while we waited for the lock; reuse its outcome
                if self.last_refresh_failed:
                    raise CrmError(f"CRM refresh failed: {self.last_error}")
                return {e: len(ix.records) for e, ix in self._by_entity.items()}
            try:
                return await self._refresh_locked()
            finally:
                self._attempts_completed += 1

    async def _refresh_locked(self) -> dict[str, int]:
        assert self._client is not None
        self.last_attempt_at = self._clock()
        counts: dict[str, int] = {}
        errors: list[str] = []
        for entity in self.entities:
            try:
                raw = await self._client.get_list(entity)
            except CrmError as exc:
                errors.append(f"{entity}: {exc}")
                log.warning("CRM refresh failed for %s: %s", entity, exc)
                continue
            # phone parsing for thousands of records is CPU work: keep it off the event loop
            records = await asyncio.to_thread(self._build_records, entity, raw)
            self._by_entity[entity] = _EntityIndex(entity, records, self._clock(), len(raw))
            counts[entity] = len(records)
        if not counts:
            self.last_refresh_failed = True
            self.last_error = "; ".join(errors) or "no entities configured"
            raise CrmError(f"CRM refresh failed for every entity: {self.last_error}")
        self._rebuild()
        self.last_refresh_at = self._clock()
        self.last_refresh_failed = False
        self.last_error = "; ".join(errors) if errors else None
        log.info("CRM index refreshed: %s", counts)
        return counts

    def _build_records(self, entity: str, raw: list[dict[str, Any]]) -> list[CrmRecord]:
        records: list[CrmRecord] = []
        for obj in raw:
            rec = record_from_api(entity, obj, self._region)
            if rec is None:
                continue
            if not rec.active and not self._include_inactive:
                continue
            records.append(rec)
        return records

    def _rebuild(self) -> None:
        by_e164: dict[str, list[MatchCandidate]] = {}
        by_suffix: dict[str, list[MatchCandidate]] = {}
        for entity in self.entities:
            ix = self._by_entity.get(entity)
            if not ix:
                continue
            for rec in ix.records:
                for field_name, raw, key in rec.phones:
                    if key.e164:
                        by_e164.setdefault(key.e164, []).append(
                            MatchCandidate(rec, field_name, raw, exact=True)
                        )
                    if key.suffix:
                        by_suffix.setdefault(key.suffix, []).append(
                            MatchCandidate(rec, field_name, raw, exact=False)
                        )
        self._by_e164 = by_e164
        self._by_suffix = by_suffix

    async def run_refresher(self, stop: asyncio.Event) -> None:
        """Background loop: refresh on a schedule with a little jitter until ``stop`` is set."""
        while not stop.is_set():
            delay = self._refresh_seconds * random.uniform(0.9, 1.1)  # noqa: S311 - jitter, not security
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await self.refresh()
            except Exception as exc:  # keep the loop alive whatever happens
                log.warning("scheduled CRM refresh failed: %s", exc)

    # ------------------------------------------------------------------ lookup
    async def lookup(self, from_phone: str | None) -> LookupResult:
        if not self.enabled:
            return LookupResult(LookupStatus.DISABLED, reason="CRM lookup is disabled")
        key = whatsapp_phone_key(from_phone)
        if key is None:
            return LookupResult(
                LookupStatus.NO_PHONE,
                reason="WhatsApp did not include the sender's phone number",
                index_age_seconds=self.index_age(),
                entities_searched=list(self.entities),
            )
        number = key.e164 or key.digits
        if self.last_refresh_at is None:
            if self._refresh_allowed():
                try:
                    await self.refresh()
                except CrmError as exc:
                    log.warning("CRM refresh failed with no cached index: %s", exc)
            if self.last_refresh_at is None:
                return LookupResult(
                    LookupStatus.ERROR,
                    reason=(
                        f"CRM unavailable and no cached customer list yet: {self.last_error or 'not loaded'}"
                    ),
                    entities_searched=list(self.entities),
                    lookup_key=number,
                )
        candidates = self._find(key)
        if not candidates:
            age = self.index_age()
            if age is not None and age >= self._miss_min_age and self._refresh_allowed():
                try:
                    await self.refresh()
                except CrmError as exc:
                    log.warning("on-miss CRM refresh failed: %s", exc)
                candidates = self._find(key)
        ranked = rank_candidates(candidates, self.entities)
        if not ranked:
            if self.last_refresh_failed:
                minutes = int((self.index_age() or 0) // 60)
                return LookupResult(
                    LookupStatus.ERROR,
                    reason=(
                        f"{number} is not in the cached customer list ({minutes} min old) and the CRM"
                        f" could not be reached to refresh it: {self.last_error}"
                    ),
                    index_age_seconds=self.index_age(),
                    entities_searched=list(self.entities),
                    lookup_key=number,
                )
            return LookupResult(
                LookupStatus.NO_MATCH,
                reason=f"no CRM record has phone {number}",
                index_age_seconds=self.index_age(),
                entities_searched=list(self.entities),
                lookup_key=number,
            )
        reply_to = self._reply_to_for(ranked)
        if not reply_to:
            return LookupResult(
                LookupStatus.NO_EMAIL,
                candidates=ranked,
                reason=f"{ranked[0].record.label} has no email address in the CRM",
                index_age_seconds=self.index_age(),
                entities_searched=list(self.entities),
                lookup_key=number,
            )
        return LookupResult(
            LookupStatus.MATCHED,
            candidates=ranked,
            reply_to=reply_to,
            index_age_seconds=self.index_age(),
            entities_searched=list(self.entities),
            lookup_key=key.e164 or key.digits,
        )

    def _refresh_allowed(self) -> bool:
        """At most one refresh attempt per CRM_MISS_REFRESH_MIN_AGE_SECONDS, successful or not.

        Without this, a failing CRM would be called again for every message from an unknown number,
        blocking the worker for the request timeout each time and repeating failed logins.
        """
        if self._lock.locked():
            return True  # a refresh is in flight: waiting for it costs no extra CRM call
        if self.last_attempt_at is None:
            return True
        return self._clock() - self.last_attempt_at >= self._miss_min_age

    def _find(self, key) -> list[MatchCandidate]:
        found: list[MatchCandidate] = []
        if key.e164 and key.e164 in self._by_e164:
            found.extend(self._by_e164[key.e164])
        if not found and key.suffix and key.suffix in self._by_suffix:
            found.extend(self._by_suffix[key.suffix])
        return found

    def _reply_to_for(self, ranked: list[MatchCandidate]) -> list[str]:
        best = ranked[0].record
        emails = list(best.emails)
        if not emails:
            return []
        return emails if self._reply_to_all else emails[:1]
