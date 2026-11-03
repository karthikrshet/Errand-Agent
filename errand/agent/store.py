"""Agent-side persistence (separate database from the provider's ledger).

The guarantees that matter live in the schema, not in Python:
* `grants.consumed_at` is set with a conditional UPDATE inside `BEGIN IMMEDIATE`, and the
  intent row is created in the same transaction. Two concurrent confirmations therefore cannot
  both proceed: exactly one sees `rowcount == 1`.
* `booking_intents.idempotency_key` is UNIQUE, so a duplicate intent is impossible even if the
  per-session lock were bypassed.
* At most one *active* grant per fingerprint (partial unique index).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from errand.agent.approval import ApprovalGrant, ApprovalRequest, idempotency_key_for
from errand.agent.state import WorkflowState
from errand.db import ConnectionPool, transaction
from errand.errors import ErrandError

IntentStatus = Literal["PENDING", "SUCCEEDED", "FAILED"]
ApprovalStatus = Literal["pending", "invalidated", "granted", "rejected"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, turn_id);
CREATE TABLE IF NOT EXISTS approvals (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES approvals(request_id),
    fingerprint TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source_turn_id INTEGER,
    issued_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS grants_one_active
    ON grants(fingerprint) WHERE consumed_at IS NULL;
CREATE TABLE IF NOT EXISTS proactive_seen (
    user_id TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_id, booking_id)
);
CREATE TABLE IF NOT EXISTS proactive_notified (
    user_id TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (user_id, booking_id)
);
CREATE TABLE IF NOT EXISTS session_leases (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_intents (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    approval_fingerprint TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    request TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class StaleState(ErrandError):
    """Another process saved this session after we loaded it."""


class BookingIntent(BaseModel):
    intent_id: str
    session_id: str
    user_id: str
    action: str
    idempotency_key: str
    approval_fingerprint: str
    grant_id: str
    request: ApprovalRequest
    status: IntentStatus
    result: dict[str, str] | None = None


class Turn(BaseModel):
    turn_id: int
    role: Literal["user", "assistant"]
    text: str


class AgentStore:
    def __init__(self, db: Path | str | sqlite3.Connection) -> None:
        self._pool = ConnectionPool(db)
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._pool.get()

    def next_boot(self) -> int:
        """Increment and return the process boot counter (seeds the ID factory)."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO meta VALUES ('boot', 1) "
                "ON CONFLICT(key) DO UPDATE SET value = value + 1"
            )
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'boot'").fetchone()
        return int(row["value"])

    # ------------------------------------------------------------------ sessions / turns

    def save_state(self, state: WorkflowState, now: datetime) -> None:
        """Compare-and-swap on `version`: a writer holding a stale snapshot is rejected
        instead of silently overwriting a newer state (see docs/BUGS.md #2)."""
        new = state.model_copy(update={"version": state.version + 1})
        with transaction(self.conn):
            if state.version == 0:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, 1, ?)",
                    (state.session_id, state.user_id, new.model_dump_json(), now.isoformat()),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE sessions SET state = ?, version = version + 1, updated_at = ? "
                    "WHERE session_id = ? AND version = ?",
                    (new.model_dump_json(), now.isoformat(), state.session_id, state.version),
                )
            if cur.rowcount != 1:
                raise StaleState(state.session_id)
        state.version = new.version

    def load_state(self, session_id: str) -> WorkflowState | None:
        row = self.conn.execute(
            "SELECT state FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else WorkflowState.model_validate_json(row["state"])

    def sessions_for(self, user_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT session_id FROM sessions WHERE user_id = ? ORDER BY updated_at", (user_id,)
        ).fetchall()
        return [str(r["session_id"]) for r in rows]

    def add_turn(self, session_id: str, user_id: str, role: str, text: str, now: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO turns (session_id, user_id, role, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, user_id, role, text, now.isoformat()),
        )
        return int(cur.lastrowid or 0)

    def turns(self, user_id: str, limit: int | None = None) -> list[Turn]:
        """All turns for a user across sessions (oldest first); `limit` keeps the newest N."""
        sql = "SELECT turn_id, role, text FROM turns WHERE user_id = ? ORDER BY turn_id DESC"
        args: tuple[object, ...] = (user_id,)
        if limit is not None:
            sql += " LIMIT ?"
            args = (user_id, limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [Turn(turn_id=r["turn_id"], role=r["role"], text=r["text"]) for r in reversed(rows)]

    def scrub_turns(self, user_id: str, values: list[str]) -> int:
        """Remove forgotten values from stored conversation (whole words only; see
        memory/forgetting.py for why short and numeric values are not scrubbed)."""
        from errand.memory.forgetting import scrub_text, scrubbable  # noqa: PLC0415

        targets = scrubbable(values)
        if not targets:
            return 0
        changed = 0
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT turn_id, text FROM turns WHERE user_id = ?", (user_id,)
            ).fetchall()
            for row in rows:
                new = scrub_text(row["text"], targets)
                if new != row["text"]:
                    self.conn.execute(
                        "UPDATE turns SET text = ? WHERE turn_id = ?", (new, row["turn_id"])
                    )
                    changed += 1
        return changed

    # ------------------------------------------------------------------ approvals / grants

    def save_approval(self, req: ApprovalRequest, session_id: str) -> None:
        self.conn.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)",
            (
                req.request_id,
                session_id,
                req.user_id,
                req.fingerprint,
                req.model_dump_json(),
                req.created_at.isoformat(),
            ),
        )

    def set_approval_status(self, request_id: str, status: ApprovalStatus, reason: str) -> None:
        self.conn.execute(
            "UPDATE approvals SET status = ?, reason = ? WHERE request_id = ? "
            "AND status = 'pending'",
            (status, reason, request_id),
        )

    def approval_status(self, request_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM approvals WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else str(row["status"])

    def grant_and_open_intent(
        self,
        req: ApprovalRequest,
        *,
        session_id: str,
        source_turn_id: int,
        now: datetime,
        grant_id: str,
        intent_id: str,
    ) -> tuple[ApprovalGrant, BookingIntent] | None:
        """ONE transaction: pending approval -> granted, grant recorded as consumed, PENDING
        intent written. Returns None if the approval is no longer pending (a duplicate or
        concurrent confirmation, or an invalidated request), so it can never act twice.

        Issuing and consuming atomically means a grant can never be left issued-but-unused by
        a crash or a lost race (docs/BUGS.md #4).
        """
        key = idempotency_key_for(req.user_id, req.fingerprint)
        grant = ApprovalGrant(
            grant_id=grant_id,
            request_id=req.request_id,
            fingerprint=req.fingerprint,
            user_id=req.user_id,
            issued_at=now,
        )
        intent = BookingIntent(
            intent_id=intent_id,
            session_id=session_id,
            user_id=req.user_id,
            action=req.action,
            idempotency_key=key,
            approval_fingerprint=req.fingerprint,
            grant_id=grant_id,
            request=req,
            status="PENDING",
        )
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE approvals SET status = 'granted' WHERE request_id = ? "
                "AND status = 'pending' AND fingerprint = ? AND user_id = ?",
                (req.request_id, req.fingerprint, req.user_id),
            )
            if cur.rowcount != 1:
                return None
            self.conn.execute(
                "INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?)",
                (grant_id, req.request_id, req.fingerprint, req.user_id, source_turn_id,
                 now.isoformat(), now.isoformat()),
            )  # fmt: skip
            self.conn.execute(
                "INSERT INTO booking_intents VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, ?, ?)",
                (intent_id, session_id, req.user_id, req.action, key, req.fingerprint,
                 grant_id, req.model_dump_json(), now.isoformat(), now.isoformat()),
            )  # fmt: skip
        return grant, intent

    # ------------------------------------------------------------------ proactive watcher

    def proactive_seen(self, user_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT booking_id FROM proactive_seen WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {str(r["booking_id"]) for r in rows}

    def mark_proactive_notified(self, user_id: str, booking_id: str, now: datetime) -> bool:
        """Record that the user was told (mid-task). True only the first time."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO proactive_notified VALUES (?, ?, ?)",
            (user_id, booking_id, now.isoformat()),
        )
        return cur.rowcount == 1

    def mark_proactive_seen(self, user_id: str, booking_id: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO proactive_seen VALUES (?, ?, ?)",
            (user_id, booking_id, now.isoformat()),
        )

    # ------------------------------------------------------------------ session lease

    def acquire_lease(self, session_id: str, owner: str, now: datetime, ttl_s: float) -> bool:
        """Serialise turns on one session across processes. Expired leases can be taken over
        (that is how a crashed holder is recovered); correctness never depends on this."""
        expires = (now + timedelta(seconds=ttl_s)).isoformat()
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT owner, expires_at FROM session_leases WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["owner"] != owner and row["expires_at"] > now.isoformat():
                return False
            self.conn.execute(
                "INSERT INTO session_leases VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE "
                "SET owner = excluded.owner, expires_at = excluded.expires_at",
                (session_id, owner, expires),
            )
        return True

    def lease_active(self, session_id: str, now: datetime) -> bool:
        """True while a turn is live on `session_id` (any intent it opened may be in flight)."""
        row = self.conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id = ? AND expires_at > ?",
            (session_id, now.isoformat()),
        ).fetchone()
        return row is not None

    def release_lease(self, session_id: str, owner: str) -> None:
        self.conn.execute(
            "DELETE FROM session_leases WHERE session_id = ? AND owner = ?", (session_id, owner)
        )

    # ------------------------------------------------------------------ intents

    def set_intent(
        self, intent_id: str, status: IntentStatus, result: dict[str, str], now: datetime
    ) -> None:
        self.conn.execute(
            "UPDATE booking_intents SET status = ?, result = ?, updated_at = ? WHERE intent_id = ?",
            (status, json.dumps(result), now.isoformat(), intent_id),
        )

    def intent_by_key(self, key: str) -> BookingIntent | None:
        row = self.conn.execute(
            "SELECT * FROM booking_intents WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else _intent(row)

    def intent(self, intent_id: str) -> BookingIntent | None:
        row = self.conn.execute(
            "SELECT * FROM booking_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else _intent(row)

    def pending_intents(self, user_id: str) -> list[BookingIntent]:
        rows = self.conn.execute(
            "SELECT * FROM booking_intents WHERE user_id = ? AND status = 'PENDING' "
            "ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [_intent(r) for r in rows]

    def all_intents(self) -> list[BookingIntent]:
        rows = self.conn.execute("SELECT * FROM booking_intents ORDER BY created_at").fetchall()
        return [_intent(r) for r in rows]

    def consumed_grant_fingerprints(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT fingerprint FROM grants WHERE consumed_at IS NOT NULL"
        ).fetchall()
        return {str(r["fingerprint"]) for r in rows}


def _intent(row: sqlite3.Row) -> BookingIntent:
    return BookingIntent(
        intent_id=row["intent_id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        action=row["action"],
        idempotency_key=row["idempotency_key"],
        approval_fingerprint=row["approval_fingerprint"],
        grant_id=row["grant_id"],
        request=ApprovalRequest.model_validate_json(row["request"]),
        status=row["status"],
        result=None if row["result"] is None else json.loads(row["result"]),
    )
