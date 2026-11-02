"""Default `book` outcome distribution, drawn through the real `book` code path.

Each seed gets its own independent `random.Random`. Tolerance is 4.5 binomial standard
deviations, so a correct implementation fails with probability ~1e-5 per proportion.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from datetime import datetime, timedelta

import pytest

from errand.clock import FakeClock
from errand.db import connect
from errand.domain.time import ist, to_ist
from errand.errors import ToolTimeout
from errand.tools.catalog import CATALOG
from errand.tools.mock_env import MockToolEnv

N = 2000
SIGMAS = 4.5


def _unique_slots(clock: FakeClock, n: int) -> list[tuple[str, datetime]]:
    start = to_ist(clock.now()).date() + timedelta(days=10)  # past the seeded bookings
    out: list[tuple[str, datetime]] = []
    day = 0
    while len(out) < n:
        d = start + timedelta(days=day)
        out.extend(
            (p.provider_id, ist(d.year, d.month, d.day, h)) for p in CATALOG for h in p.slot_hours
        )
        day += 1
    return out[:n]


def _within(observed: int, n: int, p: float) -> bool:
    sd = math.sqrt(n * p * (1 - p))
    return abs(observed - n * p) <= SIGMAS * sd


@pytest.mark.slow
@pytest.mark.parametrize("seed", [1, 2026, 424242])
def test_book_outcome_distribution(seed: int) -> None:
    clock = FakeClock()
    env = MockToolEnv(connect(":memory:"), user_id="u_demo", clock=clock, rng=random.Random(seed))
    counts: Counter[str] = Counter()
    for i, (pid, slot) in enumerate(_unique_slots(clock, N)):
        hold = env.hold_slot(pid, slot)
        key = f"dist-{i}"
        try:
            counts[env.book(hold.hold_id, key).outcome] += 1
        except ToolTimeout:
            booked = env.get_booking_status(idempotency_key=key).status == "booked"
            counts["timeout_booked" if booked else "timeout_not_booked"] += 1

    timeouts = counts["timeout_booked"] + counts["timeout_not_booked"]
    assert _within(counts["booked"], N, 0.70), counts
    assert _within(counts["slot_taken"], N, 0.15), counts
    assert _within(timeouts, N, 0.15), counts
    assert _within(counts["timeout_booked"], timeouts, 0.5), counts
    assert counts["hold_expired"] == 0
