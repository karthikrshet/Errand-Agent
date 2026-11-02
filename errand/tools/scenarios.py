"""Named scenario configs for the mock world.

A scenario can *force* outcomes (consumed FIFO) so evals are exact; once a forced queue is empty
the world falls back to the seeded default probabilities. This keeps one code path for both
modes: the distribution test and the eval scenarios exercise the same `book` implementation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"]
InjectedFailure = Literal["timeout", "503", "malformed", "crash"]


class Scenario(BaseModel):
    name: str = "default"
    book_outcomes: list[BookOutcome] = Field(default_factory=list)
    # Added to the provider's search price when quoting a hold (per provider, or "*" for all).
    hold_price_delta_paise: dict[str, int] = Field(default_factory=dict)
    reschedule_conflict_slots: list[datetime] = Field(default_factory=list)
    cancel_first_call_503: bool = True
    # Per-tool queue of failures injected *before* the tool runs (no side effect happens).
    injected_failures: dict[str, list[InjectedFailure]] = Field(default_factory=dict)
    chaos: bool = False

    def price_delta(self, provider_id: str) -> int:
        return self.hold_price_delta_paise.get(provider_id, self.hold_price_delta_paise.get("*", 0))


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(name="default"),
        Scenario(name="happy_path", book_outcomes=["success"]),
        Scenario(name="timeout_after_success", book_outcomes=["timeout_booked"]),
        Scenario(name="timeout_not_booked", book_outcomes=["timeout_not_booked", "success"]),
        Scenario(name="slot_taken", book_outcomes=["slot_taken", "success"]),
        Scenario(
            name="price_change", book_outcomes=["success"], hold_price_delta_paise={"*": 25000}
        ),
        Scenario(name="chaos", chaos=True),
    )
}


def get_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name].model_copy(deep=True)
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}") from None
