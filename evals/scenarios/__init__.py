"""Scenario registry (sorted by scenario id)."""

from __future__ import annotations

from evals.harness import EvalScenario
from evals.scenarios import booking, memory, proactive, safety


def all_scenarios() -> list[EvalScenario]:
    found = [*booking.SCENARIOS, *safety.SCENARIOS, *memory.SCENARIOS, *proactive.SCENARIOS]
    ids = [s.sid for s in found]
    assert len(ids) == len(set(ids)), f"duplicate scenario ids: {ids}"
    return sorted(found, key=lambda s: (int(s.sid[:2]), s.sid))
