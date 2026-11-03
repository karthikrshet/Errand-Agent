"""Persistent user memory with an explicit lifecycle.

explicit  -> active immediately (the user said it)
inferred  -> pending_confirmation; never enters model context until the user confirms
correct   -> new active row; the old one becomes superseded (history kept, linked)
forget    -> every row for that key becomes forgotten AND its value is scrubbed, so a forgotten
             value cannot leak later through any path that reads this table

Memory values are data. Approval policy is not a memory key, so no memory can change it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from errand.db import ConnectionPool, transaction
from errand.domain.request import ServiceRequest
from errand.memory.context import Exclusion, MemoryView

Kind = Literal["explicit", "inferred"]
Status = Literal["active", "pending_confirmation", "superseded", "forgotten"]

# Keys that can shape a booking request. Anything else (e.g. a cooking preference, free notes)
# is stored and shown to the user, but never sent to the model for booking.
BOOKING_KEYS = frozenset(
    {
        "preferred_area",
        "earliest_hour",
        "latest_hour",
        "time_of_day",
        "preferred_provider",
        "blocked_provider",
        "budget_ceiling",
        "language",
    }
)
SCRUBBED = "[forgotten]"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    service TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes_id TEXT
);
CREATE INDEX IF NOT EXISTS memories_by_user ON memories(user_id, key);
"""


class MemoryRecord(BaseModel):
    memory_id: str
    user_id: str
    key: str
    value: str
    service: str | None
    kind: Kind
    status: Status
    confidence: float
    evidence: str | None
    created_at: datetime
    updated_at: datetime
    supersedes_id: str | None

    def view(self) -> MemoryView:
        return MemoryView(
            memory_id=self.memory_id,
            key=self.key,
            value=self.value,
            kind=self.kind,
            status=self.status,
            service=self.service,
        )


class MemoryStore:
    def __init__(self, db: Path | str | sqlite3.Connection) -> None:
        self._pool = ConnectionPool(db)
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes

    @property
    def conn(self) -> sqlite3.Connection:
        return self._pool.get()

    def remember(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        memory_id: str,
        now: datetime,
        service: str | None = None,
        evidence: str | None = None,
    ) -> MemoryRecord:
        """Explicit fact: active now; supersedes any active value for the same key/service."""
        with transaction(self.conn):
            old = self._active(user_id, key, service)
            if old is not None and old.value == value:
                return old
            if old is not None:
                self._set_status(old.memory_id, "superseded", now)
            self._write(MemoryRecord(
                memory_id=memory_id, user_id=user_id, key=key, value=value, service=service,
                kind="explicit", status="active", confidence=1.0, evidence=evidence,
                created_at=now, updated_at=now, supersedes_id=old.memory_id if old else None,
            ))  # fmt: skip
        return self.get(memory_id)

    def infer(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        memory_id: str,
        now: datetime,
        confidence: float,
        evidence: str,
        service: str | None = None,
    ) -> MemoryRecord | None:
        """Inferred fact: pending until the user confirms. No duplicates, no overriding facts."""
        existing = [
            m
            for m in self.records(user_id)
            if m.key == key and m.service == service and m.status != "forgotten"
        ]
        if any(m.status in ("active", "pending_confirmation") for m in existing):
            return None
        self._write(MemoryRecord(
            memory_id=memory_id, user_id=user_id, key=key, value=value, service=service,
            kind="inferred", status="pending_confirmation", confidence=confidence,
            evidence=evidence, created_at=now, updated_at=now, supersedes_id=None,
        ))  # fmt: skip
        return self.get(memory_id)

    def confirm(self, user_id: str, memory_id: str, now: datetime) -> MemoryRecord | None:
        cur = self.conn.execute(
            "UPDATE memories SET status = 'active', updated_at = ? WHERE id = ? AND user_id = ? "
            "AND status = 'pending_confirmation'",
            (now.isoformat(), memory_id, user_id),
        )
        return self.get(memory_id) if cur.rowcount == 1 else None

    def forget(self, user_id: str, key: str, now: datetime) -> list[str]:
        """Forget every version of `key` and scrub the stored values. Returns the old values
        (so callers can scrub them from other stores too); they are not kept here."""
        rows = [m for m in self.records(user_id) if m.key == key and m.status != "forgotten"]
        values = sorted({m.value for m in rows if m.value != SCRUBBED})
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE memories SET status = 'forgotten', value = ?, evidence = NULL, "
                "updated_at = ? WHERE user_id = ? AND key = ?",
                (SCRUBBED, now.isoformat(), user_id, key),
            )
        return values

    # ------------------------------------------------------------------ reads

    def get(self, memory_id: str) -> MemoryRecord:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _record(row)

    def records(self, user_id: str) -> list[MemoryRecord]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at, id", (user_id,)
        ).fetchall()
        return [_record(r) for r in rows]

    all_records = records

    def active(self, user_id: str) -> list[MemoryRecord]:
        return [m for m in self.records(user_id) if m.status == "active"]

    def pending(self, user_id: str) -> list[MemoryRecord]:
        return [m for m in self.records(user_id) if m.status == "pending_confirmation"]

    def select_for_context(
        self, user_id: str, request: ServiceRequest
    ) -> tuple[list[MemoryView], list[Exclusion]]:
        """Only active (explicit or confirmed), booking-relevant memories may reach the model."""
        included: list[MemoryView] = []
        excluded: list[Exclusion] = []
        for m in self.records(user_id):
            reason = _exclusion_reason(m, request)
            if reason is None:
                included.append(m.view())
            else:
                excluded.append(Exclusion(memory_id=m.memory_id, reason=reason))
        return included, excluded

    # ------------------------------------------------------------------ internals

    def _active(self, user_id: str, key: str, service: str | None) -> MemoryRecord | None:
        for m in self.records(user_id):
            if m.key == key and m.service == service and m.status == "active":
                return m
        return None

    def _set_status(self, memory_id: str, status: Status, now: datetime) -> None:
        self.conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (status, now.isoformat(), memory_id),
        )

    def _write(self, m: MemoryRecord) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.memory_id, m.user_id, m.key, m.value, m.service, m.kind, m.status, m.confidence,
             m.evidence, m.created_at.isoformat(), m.updated_at.isoformat(), m.supersedes_id),
        )  # fmt: skip

    def insert_raw(self, records: Sequence[MemoryRecord]) -> None:
        """Seed loader: write records exactly as given (used for evals/seed)."""
        with transaction(self.conn):
            for m in records:
                self._write(m)


def _exclusion_reason(m: MemoryRecord, request: ServiceRequest) -> str | None:
    if m.status == "forgotten":
        return "forgotten"
    if m.status == "pending_confirmation":
        return "unconfirmed"
    if m.status == "superseded":
        return "superseded"
    if m.key not in BOOKING_KEYS:
        return "irrelevant"
    if m.service is not None and m.service != request.service:
        return "irrelevant"
    return None


def _record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["id"],
        user_id=row["user_id"],
        key=row["key"],
        value=row["value"],
        service=row["service"],
        kind=row["kind"],
        status=row["status"],
        confidence=row["confidence"],
        evidence=row["evidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        supersedes_id=row["supersedes_id"],
    )
