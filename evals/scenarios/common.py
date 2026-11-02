"""Shared fixtures for eval scenarios. FakeClock 'today' is Mon 2 Nov 2026 (14:30 IST)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from errand.agent.state import State
from errand.domain.time import to_ist
from errand.llm.script import nlu, select
from evals.harness import EvalRun, Say

TODAY, SUNDAY = "2026-11-02", "2026-11-08"
TUE, FRI, SAT = "2026-11-03", "2026-11-06", "2026-11-07"

PLUMBER_TEXT = (
    "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500. I'm in Indiranagar."
)
PLUMBER_NLU = nlu(
    "request",
    service="plumber",
    job="kitchen tap",
    area="Indiranagar",
    date_start=TODAY,
    date_end=SUNDAY,
    earliest_hour=8,
    latest_hour=12,
    budget_rupees=1500,
)


def ask_plumber() -> Say:
    return Say(PLUMBER_TEXT, PLUMBER_NLU)


def pick(n: int, hour: int | None = None) -> Say:
    return Say(f"book option {n}" + (f" at {hour}" if hour is not None else ""), select(n, hour))


def pick_provider(provider_id: str, hour: int | None = None) -> Say:
    sel: dict[str, Any] = {"provider_id": provider_id}
    if hour is not None:
        sel["hour"] = hour
    return Say(f"book {provider_id}", nlu("select_option", selection=sel))


YES = Say("yes")


class Checks:
    """Collects failed expectations so a scenario reports all of them, not just the first."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def that(self, cond: object, message: str) -> None:
        if not cond:
            self.failures.append(message)


def checks(fn: Callable[[EvalRun, Checks], None]) -> Callable[[EvalRun], list[str]]:
    def run(r: EvalRun) -> list[str]:
        c = Checks()
        try:
            fn(r, c)
        except Exception as exc:  # a crashing check is a failing check
            c.failures.append(f"check raised {type(exc).__name__}: {exc}")
        return c.failures

    return run


def final_state(r: EvalRun) -> State:
    return r.world.agent.load("eval").status


def hours(slots: list[Any]) -> list[int]:
    return [to_ist(s).hour for s in slots]
