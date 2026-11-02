"""Per-run context shared by the flows: state, tools, store, tracer, limits.

`move` is the only way flows change workflow state: it validates against the transition table,
traces the transition with its trigger, and persists immediately so a restart resumes exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from errand.agent.approval import ApprovalRequest
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


def leaves_approval(before: State, after: State) -> bool:
    """A card the user is no longer looking at must never be confirmable (BUGS.md #7)."""
    return before == State.AWAITING_APPROVAL and after != State.EXECUTING


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
        stale = candidate.pending_approval if leaves_approval(before, after) else None
        if stale is not None:
            candidate.pending_approval = None
        self.store.save_state(candidate, self.clock.now())
        self.state.status = candidate.status
        self.state.version = candidate.version
        self.tracer.emit(
            "state.transition",
            session_id=self.state.session_id,
            from_state=before.value,
            to_state=after.value,
            trigger=trigger,
            reason=reason,
        )
        if stale is not None:
            self.state.pending_approval = None
            self.invalidate(stale, f"left approval for {after.value}")

    def invalidate(self, req: ApprovalRequest, reason: str) -> None:
        """Mark an approval request unusable (DB + trace). Single place for this rule."""
        self.store.set_approval_status(req.request_id, "invalidated", reason)
        self.tracer.emit(
            "approval.invalidated",
            request_id=req.request_id,
            fingerprint=req.fingerprint,
            reason=reason,
        )

    def say(self, text: str) -> None:
        self.replies.append(text)

    def save(self) -> None:
        self.store.save_state(self.state, self.clock.now())

    def tool(self, name: str, args: BaseModel, out: type[T]) -> T:
        self.state.counters.tool_calls += 1
        return self.gateway.call(name, args, out, state=self.state.status)
