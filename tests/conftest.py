from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import pytest

from errand.clock import FakeClock
from errand.tools.mock_env import MockToolEnv
from errand.tools.scenarios import Scenario

EnvFactory = Callable[..., MockToolEnv]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def make_env(ledger_path: Path, clock: FakeClock) -> EnvFactory:
    def factory(
        scenario: Scenario | None = None, user_id: str = "u_demo", seed: int = 7
    ) -> MockToolEnv:
        return MockToolEnv(
            ledger_path,
            user_id=user_id,
            clock=clock,
            rng=random.Random(seed),
            scenario=scenario,
        )

    return factory
