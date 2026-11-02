"""Time as an injected dependency.

Hold expiry, wall-clock limits and retry backoff all depend on time. Reading the system clock
directly would make those paths untestable, so logic only ever talks to a `Clock`.
`FakeClock.sleep` advances virtual time instantly, which keeps retry-heavy evals fast.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware current time (UTC)."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware start time")
        self._now = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot sleep a negative duration")
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
