"""Seed booking history so "same electrician as last time" and "move my AC servicing" work.

Dates are relative to the world's 'today' so the demo works on any day; evals pin today with a
FakeClock, so they stay deterministic. Seeding is idempotent (INSERT OR IGNORE on fixed IDs).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from errand.domain.time import iso, ist
from errand.tools.catalog import BY_ID

# (booking_id, user_id, provider_id, day offset from today, hour, status)
SEED_BOOKINGS: tuple[tuple[str, str, str, int, int, str], ...] = (
    ("b_hist_pl03", "u_demo", "pl_aqua", -133, 8, "upcoming"),
    ("b_hist_pl02", "u_demo", "pl_quickfix", -91, 10, "upcoming"),
    ("b_hist_cl01", "u_demo", "cl_fresh", -70, 14, "cancelled"),
    ("b_hist_pl01", "u_demo", "pl_aqua", -50, 9, "upcoming"),
    ("b_hist_el01", "u_demo", "el_ravi", -21, 9, "upcoming"),
    ("b_up_ac01", "u_demo", "ac_cool", 2, 15, "upcoming"),
    ("b_other_01", "u_other", "el_volt", 3, 13, "upcoming"),
)


def seed_ledger(conn: sqlite3.Connection, today: date) -> None:
    created = ist(today.year, today.month, today.day).isoformat()
    for booking_id, user_id, provider_id, offset, hour, status in SEED_BOOKINGS:
        d = today + timedelta(days=offset)
        p = BY_ID[provider_id]
        conn.execute(
            "INSERT OR IGNORE INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                booking_id,
                user_id,
                provider_id,
                p.service,
                iso(ist(d.year, d.month, d.day, hour)),
                p.base_price_paise,
                status,
                created,
            ),
        )
