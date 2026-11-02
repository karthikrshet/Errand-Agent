"""Run the unforced happy path over many seeds: every outcome path must keep the invariants."""

from __future__ import annotations

import dataclasses
from collections import Counter
from pathlib import Path

from evals.harness import execute
from evals.invariants import check_all
from evals.scenarios import all_scenarios


def test_default_odds_across_seeds(tmp_path: Path) -> None:
    base = next(s for s in all_scenarios() if s.sid == "34")
    outcomes: Counter[str] = Counter()
    for seed in range(40):
        sc = dataclasses.replace(base, seed=seed)
        run = execute(sc, tmp_path / str(seed))
        failures = sc.check(run) + [m for v in check_all(run).values() for m in v]
        assert not failures, (seed, failures)
        tools = [str(x.get("outcome")) for x in run.tool_calls("book")]
        outcomes["timeout" if "error" in tools else run.world.agent.load("eval").status.value] += 1
    # The seeds must actually exercise more than the happy path.
    assert outcomes["COMPLETED"] > 0
    assert len(outcomes) >= 2, outcomes
