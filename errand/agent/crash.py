"""Test-only crash injection at named points, to prove restart recovery at each one.

`SimulatedCrash` derives from BaseException so the orchestrator's top-level guard (which
catches Exception) does not swallow it: it behaves like the process dying mid-step.
"""

from __future__ import annotations

from typing import Literal

CrashPoint = Literal[
    "after_intent_write",
    "after_book_call_before_response",
    "after_booking_before_state_save",
]
CRASH_POINTS: tuple[CrashPoint, ...] = (
    "after_intent_write",
    "after_book_call_before_response",
    "after_booking_before_state_save",
)


class SimulatedCrash(BaseException):
    def __init__(self, point: str) -> None:
        super().__init__(f"simulated crash at {point}")
        self.point = point


class CrashHooks:
    def __init__(self, armed: set[CrashPoint] | None = None) -> None:
        self.armed: set[CrashPoint] = set(armed or ())

    def hit(self, point: CrashPoint) -> None:
        if point in self.armed:
            self.armed.discard(point)
            raise SimulatedCrash(point)
