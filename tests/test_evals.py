"""Every eval scenario also runs under pytest, so `make test` gates on scenarios + invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness import EvalScenario, execute
from evals.invariants import check_all
from evals.scenarios import all_scenarios


@pytest.mark.parametrize("sc", all_scenarios(), ids=lambda s: f"{s.sid}-{s.name}")
def test_scenario(sc: EvalScenario, tmp_path: Path) -> None:
    run = execute(sc, tmp_path / sc.sid)
    failures = sc.check(run) + [f"{k}: {m}" for k, msgs in check_all(run).items() for m in msgs]
    assert not failures, failures


def test_at_least_twenty_scenarios() -> None:
    assert len(all_scenarios()) >= 20
