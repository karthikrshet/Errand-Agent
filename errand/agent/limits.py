"""Hard limits for one run (one user turn, or one proactive check).

Every limit check raises `LimitExceeded`; the orchestrator's top-level guard turns that into a
user-facing message and a saved state, so no limit can produce a hang or a stack trace.
"""

from __future__ import annotations

from datetime import datetime

from errand.clock import Clock
from errand.config import Settings
from errand.errors import LimitExceeded


class RunBudget:
    def __init__(self, settings: Settings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock
        self.started_at: datetime = clock.now()
        self.tool_calls = 0
        self.model_calls = 0

    def elapsed_s(self) -> float:
        return (self.clock.now() - self.started_at).total_seconds()

    def check_time(self) -> None:
        if self.elapsed_s() > self.settings.run_wall_clock_s:
            raise LimitExceeded("run_wall_clock_s", self.settings.run_wall_clock_s)

    def charge_tool(self) -> None:
        self.check_time()
        if self.tool_calls >= self.settings.max_tool_calls_per_run:
            raise LimitExceeded("max_tool_calls_per_run", self.settings.max_tool_calls_per_run)
        self.tool_calls += 1

    def charge_model(self) -> None:
        self.check_time()
        if self.model_calls >= self.settings.max_model_calls_per_turn:
            raise LimitExceeded("max_model_calls_per_turn", self.settings.max_model_calls_per_turn)
        self.model_calls += 1
