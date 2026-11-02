"""Per-run context shared by the flows: state, tools, store, tracer, limits.

`move` is the only way flows change workflow state: it validates against the transition table,
traces the transition with its trigger, and persists immediately so a restart resumes exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from errand.agent.crash import CrashHooks
from errand.agent.limits import RunBudget
from errand.agent.state import State, Trigger, WorkflowState
from errand.agent.store import AgentStore
from errand.clock import Clock
from errand.config import Settings
from errand.ids import IdFactory
from errand.llm.base import LLMClient
from errand.memory.store import MemoryStore
from errand.observability.trace import Tracer
from errand.tools.gateway import ToolGateway

T = TypeVar("T", bound=BaseModel)


@dataclass
class RunContext:
    state: WorkflowState
    store: AgentStore
    gateway: ToolGateway
    tracer: Tracer
    clock: Clock
    settings: Settings
    budget: RunBudget
    crash: CrashHooks
    ids: IdFactory
    memory: MemoryStore
    llm: LLMClient
    user_turn_id: int | None = None
    replies: list[str] = field(default_factory=list)

    @property
    def user_id(self) -> str:
        return self.state.user_id

    def move(self, to: State, trigger: Trigger, reason: str = "") -> None:
        """Validate, persist, then adopt. If persisting fails (StaleState), the in-memory
        state is left untouched, so error handling never sees a half-applied transition."""
        candidate = self.state.model_copy(deep=True)
        before, after = candidate.transition(to, trigger)
        self.store.save_state(candidate, self.clock.now())
        self.state.status = candidate.status
        self.state.version = candidate.version
        self.tracer.emit(
            "state.transition",
            from_state=before.value,
            to_state=after.value,
            trigger=trigger,
            reason=reason,
        )

    def say(self, text: str) -> None:
        self.replies.append(text)

    def save(self) -> None:
        self.store.save_state(self.state, self.clock.now())

    def tool(self, name: str, args: BaseModel, out: type[T]) -> T:
        self.state.counters.tool_calls += 1
        return self.gateway.call(name, args, out, state=self.state.status)
