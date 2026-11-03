"""The mock provider world: eight tools backed by a SQLite ledger.

This plays the role of a *remote* provider API. It has its own database (the ledger), knows the
authenticated caller (`user_id`, like an auth header, never a tool argument chosen by the LLM),
and exposes a JSON "wire" entry point (`call`) that the gateway uses, so malformed responses and
transport failures can be simulated at the boundary where a real network would produce them.

Side-effect semantics:
* `book` memoises business outcomes per idempotency key: same key => same outcome, never a second
  booking. A timeout is raised *after* the ledger may have been written (timeout_booked).
* `cancel` fails the first attempt for each key with a transient 503; the retry succeeds.
* Active bookings are unique per (provider_id, slot) via a partial unique index.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from errand.clock import Clock
from errand.config import BookOutcomeOdds, Settings
from errand.db import ConnectionPool, transaction
from errand.domain.money import Price
from errand.domain.time import days_in_range, iso, ist, parse_iso, to_ist
from errand.errors import (
    IdempotencyConflict,
    InvalidRequest,
    PermissionDenied,
    ToolTimeout,
    TransientToolError,
    UnknownResource,
)
from errand.tools import contracts as c
from errand.tools.catalog import BY_ID, CATALOG, CatalogProvider
from errand.tools.scenarios import BookOutcome, Scenario
from errand.tools.seed_ledger import seed_ledger

EXTERNAL_USER = "u_external"
MAX_RANGE_DAYS = 31
CHAOS_ODDS = BookOutcomeOdds(success=0.4, slot_taken=0.2, timeout=0.4)
CHAOS_READ_FAILURE_RATE = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS holds (
    seq INTEGER PRIMARY KEY,
    hold_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    price_paise INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    service TEXT NOT NULL,
    slot TEXT NOT NULL,
    price_paise INTEGER NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    hold_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bookings_active_slot
    ON bookings(provider_id, slot) WHERE status = 'upcoming';
CREATE TABLE IF NOT EXISTS idempotency (
    tool TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tool, key)
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    user_id TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at TEXT
);
CREATE TABLE IF NOT EXISTS key_holds (
    key TEXT PRIMARY KEY,
    hold_id TEXT NOT NULL
);
"""


def _request_hash(**fields: object) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def booking_id_for(idempotency_key: str) -> str:
    return "b_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]


class MockToolEnv:
    """One authenticated client session against the shared ledger."""

    def __init__(
        self,
        ledger: Path | str | sqlite3.Connection,
        *,
        user_id: str,
        clock: Clock,
        rng: random.Random,
        scenario: Scenario | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._pool = ConnectionPool(ledger)
        self.user_id = user_id
        self.clock = clock
        self.rng = rng
        self.scenario = scenario or Scenario()
        self.settings = settings or Settings()
        self._forced: list[BookOutcome] = list(self.scenario.book_outcomes)
        self._injected = {k: list(v) for k, v in self.scenario.injected_failures.items()}
        self.conn.executescript(SCHEMA)
        seed_ledger(self.conn, to_ist(clock.now()).date())

    # ------------------------------------------------------------------ wire API

    @property
    def conn(self) -> sqlite3.Connection:
        return self._pool.get()

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """JSON in, JSON out, like an HTTP endpoint. The gateway validates the response."""
        handler = self._handlers().get(tool)
        if handler is None:
            raise UnknownResource(f"unknown tool {tool!r}")
        injected = self._take_injected(tool)
        if injected == "malformed":
            return {"unexpected": "payload", "tool": tool}
        input_model, fn = handler
        try:
            parsed = input_model.model_validate(args)
        except ValidationError as exc:
            raise InvalidRequest(
                f"invalid arguments for {tool}: {exc.error_count()} errors"
            ) from exc
        return fn(parsed).model_dump(mode="json")

    def _handlers(self) -> dict[str, tuple[type[BaseModel], Callable[[Any], BaseModel]]]:
        return {
            "search_providers": (
                c.SearchProvidersInput,
                lambda a: self.search_providers(a.service, a.area, a.date_range),
            ),
            "get_provider": (c.GetProviderInput, lambda a: self.get_provider(a.provider_id)),
            "hold_slot": (c.HoldSlotInput, lambda a: self.hold_slot(a.provider_id, a.slot)),
            "book": (c.BookInput, lambda a: self.book(a.hold_id, a.idempotency_key)),
            "get_booking_status": (
                c.GetBookingStatusInput,
                lambda a: self.get_booking_status(a.hold_id, a.idempotency_key),
            ),
            "cancel": (c.CancelInput, lambda a: self.cancel(a.booking_id, a.idempotency_key)),
            "reschedule": (
                c.RescheduleInput,
                lambda a: self.reschedule(a.booking_id, a.slot, a.idempotency_key),
            ),
            "list_bookings": (c.ListBookingsInput, lambda a: self.list_bookings(a.user_id)),
        }

    def _take_injected(self, tool: str) -> str | None:
        queue = self._injected.get(tool)
        failure = queue.pop(0) if queue else None
        chaotic = self.scenario.chaos and tool not in c.SIDE_EFFECT_TOOLS
        if failure is None and chaotic and self.rng.random() < CHAOS_READ_FAILURE_RATE:
            failure = "503"
        if failure == "timeout":
            raise ToolTimeout(f"{tool} timed out")
        if failure == "503":
            raise TransientToolError(503)
        if failure == "crash":
            raise RuntimeError(f"provider crashed while handling {tool}")
        return failure

    # ------------------------------------------------------------------ read tools

    def search_providers(
        self, service: str, area: str, date_range: c.DateRange
    ) -> c.SearchProvidersOutput:
        if date_range.end < date_range.start:
            raise InvalidRequest("date_range.end is before date_range.start")
        if (date_range.end - date_range.start).days > MAX_RANGE_DAYS:
            raise InvalidRequest(f"date_range may span at most {MAX_RANGE_DAYS} days")
        matches = [p for p in CATALOG if p.service == service.strip().lower()]
        limit = self.settings.search_max_providers
        days = days_in_range(date_range.start, date_range.end)
        return c.SearchProvidersOutput(
            providers=tuple(self._summary(p, days) for p in matches[:limit])
        )

    def get_provider(self, provider_id: str) -> c.GetProviderOutput:
        p = self._provider(provider_id)
        today = to_ist(self.clock.now()).date()
        return c.GetProviderOutput(
            provider=self._summary(p, days_in_range(today, today + timedelta(days=6))),
            description=p.description,
            reviews=tuple(
                c.Review(author=r.author, rating=r.rating, text=r.text) for r in p.reviews
            ),
        )

    def list_bookings(self, user_id: str) -> c.ListBookingsOutput:
        if user_id != self.user_id:
            raise PermissionDenied("cannot list another user's bookings")
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY slot", (user_id,)
        ).fetchall()
        return c.ListBookingsOutput(bookings=tuple(self._record(r) for r in rows))

    def get_booking_status(
        self, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> c.BookingStatusOutput:
        if (hold_id is None) == (idempotency_key is None):
            raise InvalidRequest("pass exactly one of hold_id or idempotency_key")
        if idempotency_key is not None:
            row = self.conn.execute(
                "SELECT booking_id, status FROM bookings WHERE idempotency_key = ? AND user_id = ?",
                (idempotency_key, self.user_id),
            ).fetchone()
            if row is not None:
                return c.BookingStatusOutput(status="booked", booking_id=row["booking_id"])
            hold_id = self._hold_for_key(idempotency_key)
            if hold_id is None:
                return c.BookingStatusOutput(status="unknown_hold")
        assert hold_id is not None
        hold = self._own_hold(hold_id)
        if hold is None:
            return c.BookingStatusOutput(status="unknown_hold")
        row = self.conn.execute(
            "SELECT booking_id FROM bookings WHERE hold_id = ? AND user_id = ?",
            (hold_id, self.user_id),
        ).fetchone()
        if row is not None:
            return c.BookingStatusOutput(status="booked", booking_id=row["booking_id"])
        return c.BookingStatusOutput(status="not_booked", hold_expired=self._expired(hold))

    # ------------------------------------------------------------------ hold

    def hold_slot(self, provider_id: str, slot: datetime) -> c.HoldSlotOutput:
        p = self._provider(provider_id)
        self._validate_slot(p, slot)
        key = iso(slot)
        now = self.clock.now()
        with transaction(self.conn):
            if self._slot_booked(provider_id, key):
                raise InvalidRequest("slot is no longer available")
            existing = self._active_hold(provider_id, key)
            if existing is not None:
                if existing["user_id"] != self.user_id:
                    raise InvalidRequest("slot is currently held by another customer")
                return self._hold_out(existing)
            seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM holds").fetchone()[0]
            price = p.base_price_paise + self.scenario.price_delta(provider_id)
            expires = now + timedelta(seconds=self.settings.hold_ttl_s)
            self.conn.execute(
                "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, f"h_{seq:06d}", self.user_id, provider_id, key, price,
                 expires.isoformat(), now.isoformat()),
            )  # fmt: skip
        row = self.conn.execute("SELECT * FROM holds WHERE seq = ?", (seq,)).fetchone()
        return self._hold_out(row)

    # ------------------------------------------------------------------ book

    def book(self, hold_id: str, idempotency_key: str) -> c.BookOutput:
        req = _request_hash(user=self.user_id, hold_id=hold_id)
        memo = self._memo("book", idempotency_key, req)
        if memo is not None:
            return c.BookOutput.model_validate_json(memo)
        hold = self._own_hold(hold_id)
        if hold is None:
            raise UnknownResource(f"unknown hold {hold_id}")
        self._note_attempt("book", idempotency_key, req, hold_id)
        if self._expired(hold):
            return c.BookOutput(outcome="hold_expired", hold_id=hold_id)
        outcome = self._draw_book_outcome()
        if outcome == "timeout_not_booked":
            raise ToolTimeout("book timed out")
        if outcome == "slot_taken":
            return self._record_slot_taken(hold, idempotency_key)
        result = self._record_booking(hold, idempotency_key)
        if outcome == "timeout_booked":
            raise ToolTimeout("book timed out (the booking may have gone through)")
        return result

    def _draw_book_outcome(self) -> BookOutcome:
        if self._forced:
            return self._forced.pop(0)
        odds = CHAOS_ODDS if self.scenario.chaos else self.settings.book_odds
        u = self.rng.random()
        if u < odds.success:
            return "success"
        if u < odds.success + odds.slot_taken:
            return "slot_taken"
        booked = self.rng.random() < odds.timeout_booked_fraction
        return "timeout_booked" if booked else "timeout_not_booked"

    def _record_booking(self, hold: sqlite3.Row, key: str) -> c.BookOutput:
        p = self._provider(hold["provider_id"])
        booking_id = booking_id_for(key)
        out = c.BookOutput(outcome="booked", booking_id=booking_id, hold_id=hold["hold_id"])
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, 'upcoming', ?, ?, ?)",
                    (booking_id, self.user_id, p.provider_id, p.service, hold["slot"],
                     hold["price_paise"], key, hold["hold_id"], self.clock.now().isoformat()),
                )  # fmt: skip
                self._store_memo("book", key, out)
        except sqlite3.IntegrityError:
            return self._record_slot_taken(hold, key)
        return out

    def _record_slot_taken(self, hold: sqlite3.Row, key: str) -> c.BookOutput:
        """Someone else got the slot: persist a competitor booking so searches reflect it."""
        p = self._provider(hold["provider_id"])
        out = c.BookOutput(outcome="slot_taken", hold_id=hold["hold_id"])
        with transaction(self.conn):
            if not self._slot_booked(p.provider_id, hold["slot"]):
                self.conn.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, 'upcoming', NULL, NULL, ?)",
                    (f"b_ext_{hold['hold_id']}", EXTERNAL_USER, p.provider_id, p.service,
                     hold["slot"], p.base_price_paise, self.clock.now().isoformat()),
                )  # fmt: skip
            self._store_memo("book", key, out)
        return out

    # ------------------------------------------------------------------ cancel / reschedule

    def cancel(self, booking_id: str, idempotency_key: str) -> c.CancelOutput:
        req = _request_hash(user=self.user_id, booking_id=booking_id)
        memo = self._memo("cancel", idempotency_key, req)
        if memo is not None:
            return c.CancelOutput.model_validate_json(memo)
        booking = self._own_booking(booking_id)
        attempts = self._note_attempt("cancel", idempotency_key, req, None)
        if self.scenario.cancel_first_call_503 and attempts == 1:
            raise TransientToolError(503, "cancellation service briefly unavailable")
        if booking["status"] == "upcoming" and self._is_past(booking["slot"]):
            raise InvalidRequest("cannot cancel a booking that already happened")
        out = c.CancelOutput(outcome="cancelled", booking_id=booking_id)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE booking_id = ? "
                "AND status = 'upcoming'",
                (booking_id,),
            )
            self._store_memo("cancel", idempotency_key, out)
        return out

    def reschedule(
        self, booking_id: str, slot: datetime, idempotency_key: str
    ) -> c.RescheduleOutput:
        req = _request_hash(user=self.user_id, booking_id=booking_id, slot=iso(slot))
        memo = self._memo("reschedule", idempotency_key, req)
        if memo is not None:
            return c.RescheduleOutput.model_validate_json(memo)
        booking = self._own_booking(booking_id)
        if booking["status"] != "upcoming" or self._is_past(booking["slot"]):
            raise InvalidRequest("only upcoming bookings can be rescheduled")
        p = self._provider(booking["provider_id"])
        self._validate_slot(p, slot)
        self._note_attempt("reschedule", idempotency_key, req, None)
        key = iso(slot)
        forced = {iso(s) for s in self.scenario.reschedule_conflict_slots}
        if key in forced or self._slot_booked(p.provider_id, key):
            out = c.RescheduleOutput(
                outcome="slot_conflict",
                booking_id=booking_id,
                slot=slot,
                alternative_slots=self._alternatives(p, slot, exclude=forced),
            )
        else:
            out = c.RescheduleOutput(outcome="rescheduled", booking_id=booking_id, slot=slot)
        with transaction(self.conn):
            if out.outcome == "rescheduled":
                self.conn.execute(
                    "UPDATE bookings SET slot = ? WHERE booking_id = ?", (key, booking_id)
                )
            self._store_memo("reschedule", idempotency_key, out)
        return out

    # ------------------------------------------------------------------ mock-only hooks

    def simulate_provider_cancellation(self, booking_id: str) -> None:
        """The provider cancels on their side; emits an event the proactive watcher can see."""
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT user_id FROM bookings WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            if row is None:
                raise UnknownResource(f"unknown booking {booking_id}")
            self.conn.execute(
                "UPDATE bookings SET status = 'cancelled_by_provider' WHERE booking_id = ?",
                (booking_id,),
            )
            self.conn.execute(
                "INSERT INTO events (kind, user_id, booking_id, created_at) VALUES (?, ?, ?, ?)",
                ("provider_cancelled", row["user_id"], booking_id, self.clock.now().isoformat()),
            )

    def pending_events(self) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            "SELECT event_id, kind, booking_id FROM events WHERE user_id = ? AND acked_at IS NULL "
            "ORDER BY event_id",
            (self.user_id,),
        ).fetchall()
        return [(r["event_id"], r["kind"], r["booking_id"]) for r in rows]

    def ack_event(self, event_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET acked_at = ? WHERE event_id = ? AND user_id = ?",
            (self.clock.now().isoformat(), event_id, self.user_id),
        )

    def count_bookings(self, idempotency_key: str | None = None) -> int:
        """Test/eval helper: how many ledger rows exist (for 'exactly one booking' checks)."""
        if idempotency_key is None:
            sql, args = "SELECT COUNT(*) FROM bookings WHERE user_id = ?", (self.user_id,)
        else:
            sql, args = (
                "SELECT COUNT(*) FROM bookings WHERE idempotency_key = ?",
                (idempotency_key,),
            )
        return int(self.conn.execute(sql, args).fetchone()[0])

    # ------------------------------------------------------------------ helpers

    def _provider(self, provider_id: str) -> CatalogProvider:
        try:
            return BY_ID[provider_id]
        except KeyError:
            raise UnknownResource(f"unknown provider {provider_id}") from None

    def _summary(self, p: CatalogProvider, days: list[date]) -> c.ProviderSummary:
        now = self.clock.now()
        slots = []
        for d in days:
            for h in p.slot_hours:
                slot = ist(d.year, d.month, d.day, h)
                key = iso(slot)
                if slot > now and not self._slot_booked(p.provider_id, key):
                    hold = self._active_hold(p.provider_id, key)
                    if hold is None or hold["user_id"] == self.user_id:
                        slots.append(slot)
        return c.ProviderSummary(
            provider_id=p.provider_id,
            name=p.name,
            service=p.service,
            price=Price(paise=p.base_price_paise),
            rating=p.rating,
            available_slots=tuple(slots),
            attributes=c.ProviderAttributes(
                verified=p.verified,
                languages=p.languages,
                years_experience=p.years_experience,
                technician_name=p.technician_name,
            ),
        )

    def _validate_slot(self, p: CatalogProvider, slot: datetime) -> None:
        if slot.tzinfo is None:
            raise InvalidRequest("slot must be timezone-aware")
        local = to_ist(slot)
        if local.minute or local.second or local.hour not in p.slot_hours:
            raise InvalidRequest(f"{p.provider_id} does not offer {iso(slot)}")
        if slot <= self.clock.now():
            raise InvalidRequest("slot is in the past")

    def _slot_booked(self, provider_id: str, slot_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'upcoming'",
            (provider_id, slot_key),
        ).fetchone()
        return row is not None

    def _active_hold(self, provider_id: str, slot_key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM holds WHERE provider_id = ? AND slot = ? AND expires_at > ? "
            "ORDER BY seq DESC LIMIT 1",
            (provider_id, slot_key, self.clock.now().isoformat()),
        ).fetchone()
        return row

    def _own_hold(self, hold_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM holds WHERE hold_id = ? AND user_id = ?", (hold_id, self.user_id)
        ).fetchone()
        return row

    def _own_booking(self, booking_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)
        ).fetchone()
        # Another user's booking is indistinguishable from a missing one (no existence oracle).
        if row is None or row["user_id"] != self.user_id:
            raise UnknownResource(f"unknown booking {booking_id}")
        return row

    def _expired(self, hold: sqlite3.Row) -> bool:
        return self.clock.now() >= datetime.fromisoformat(hold["expires_at"])

    def _is_past(self, slot_key: str) -> bool:
        return parse_iso(slot_key) <= self.clock.now()

    def _hold_out(self, row: sqlite3.Row) -> c.HoldSlotOutput:
        return c.HoldSlotOutput(
            hold_id=row["hold_id"],
            provider_id=row["provider_id"],
            slot=parse_iso(row["slot"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            quoted_price=Price(paise=row["price_paise"]),
        )

    def _record(self, row: sqlite3.Row) -> c.BookingRecord:
        status = row["status"]
        if status == "upcoming" and self._is_past(row["slot"]):
            status = "completed"
        p = self._provider(row["provider_id"])
        return c.BookingRecord(
            booking_id=row["booking_id"],
            provider_id=p.provider_id,
            provider=p.name,
            service=row["service"],
            slot=parse_iso(row["slot"]),
            status=status,
            price=Price(paise=row["price_paise"]),
        )

    def _alternatives(
        self, p: CatalogProvider, near: datetime, exclude: set[str]
    ) -> tuple[datetime, ...]:
        start = to_ist(near).date()
        out: list[datetime] = []
        for d in days_in_range(start, start + timedelta(days=6)):
            for h in p.slot_hours:
                s = ist(d.year, d.month, d.day, h)
                k = iso(s)
                if (
                    s > self.clock.now()
                    and k not in exclude
                    and not self._slot_booked(p.provider_id, k)
                ):
                    out.append(s)
        return tuple(out[:3])

    def _memo(self, tool: str, key: str, request_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT request_hash, response FROM idempotency WHERE tool = ? AND key = ?",
            (tool, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict(f"{tool}: idempotency key reused with different arguments")
        response: str | None = row["response"]
        return response

    def _note_attempt(self, tool: str, key: str, request_hash: str, hold_id: str | None) -> int:
        """Persist the attempt *before* acting, so the key is known even if we then 'time out'."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO idempotency (tool, key, request_hash, attempts) VALUES (?, ?, ?, 0) "
                "ON CONFLICT(tool, key) DO NOTHING",
                (tool, key, request_hash),
            )
            self.conn.execute(
                "UPDATE idempotency SET attempts = attempts + 1 WHERE tool = ? AND key = ?",
                (tool, key),
            )
            if hold_id is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO key_holds (key, hold_id) VALUES (?, ?)", (key, hold_id)
                )
        row = self.conn.execute(
            "SELECT attempts FROM idempotency WHERE tool = ? AND key = ?", (tool, key)
        ).fetchone()
        return int(row["attempts"])

    def _hold_for_key(self, key: str) -> str | None:
        row = self.conn.execute("SELECT hold_id FROM key_holds WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["hold_id"])

    def _store_memo(self, tool: str, key: str, out: BaseModel) -> None:
        self.conn.execute(
            "UPDATE idempotency SET response = ? WHERE tool = ? AND key = ?",
            (out.model_dump_json(), tool, key),
        )
