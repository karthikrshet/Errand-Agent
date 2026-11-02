"""Explicit workflow state machine.

States and legal transitions are data (`TRANSITIONS`), not control flow scattered through the
orchestrator. Every transition records *what triggered it*; the eval invariant I6 uses that to
prove tool output never moved the workflow into a state reserved for user intent.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from errand.agent.approval import ApprovalRequest
from errand.domain.request import FieldSource, ServiceRequest
from errand.errors import IllegalTransition


class State(StrEnum):
    IDLE = "IDLE"
    GATHERING = "GATHERING"
    SEARCHING = "SEARCHING"
    PRESENTING_OPTIONS = "PRESENTING_OPTIONS"
    HOLDING = "HOLDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    CANCEL_FLOW = "CANCEL_FLOW"
    RESCHEDULE_FLOW = "RESCHEDULE_FLOW"
    PROACTIVE_REPLACEMENT = "PROACTIVE_REPLACEMENT"
    FAILED_SAFE = "FAILED_SAFE"
    ABANDONED = "ABANDONED"


S = State
TERMINAL: frozenset[State] = frozenset({S.COMPLETED, S.FAILED_SAFE, S.ABANDONED})
# Entering these requires a user message (or a grant derived from one) as the trigger.
USER_RESERVED: frozenset[State] = frozenset({S.EXECUTING, S.CANCEL_FLOW, S.RESCHEDULE_FLOW})

_ENTRY = {S.GATHERING, S.SEARCHING, S.CANCEL_FLOW, S.RESCHEDULE_FLOW}
TRANSITIONS: dict[State, frozenset[State]] = {
    S.IDLE: frozenset({*_ENTRY, S.PROACTIVE_REPLACEMENT, S.ABANDONED}),
    S.GATHERING: frozenset({*_ENTRY, S.ABANDONED, S.FAILED_SAFE}),
    S.SEARCHING: frozenset({S.PRESENTING_OPTIONS, S.GATHERING, S.FAILED_SAFE}),
    S.PRESENTING_OPTIONS: frozenset({*_ENTRY, S.HOLDING, S.ABANDONED, S.FAILED_SAFE}),
    S.HOLDING: frozenset({S.AWAITING_APPROVAL, S.PRESENTING_OPTIONS, S.FAILED_SAFE}),
    S.AWAITING_APPROVAL: frozenset(
        {*_ENTRY, S.EXECUTING, S.HOLDING, S.PRESENTING_OPTIONS, S.ABANDONED, S.FAILED_SAFE}
    ),
    S.EXECUTING: frozenset(
        {
            S.VERIFYING,
            S.COMPLETED,
            S.PRESENTING_OPTIONS,
            S.HOLDING,
            S.FAILED_SAFE,
            S.RESCHEDULE_FLOW,
            S.AWAITING_APPROVAL,
        }
    ),
    S.VERIFYING: frozenset(
        {
            S.COMPLETED,
            S.EXECUTING,
            S.PRESENTING_OPTIONS,
            S.HOLDING,
            S.AWAITING_APPROVAL,
            S.FAILED_SAFE,
        }
    ),
    S.CANCEL_FLOW: frozenset({S.AWAITING_APPROVAL, S.GATHERING, S.FAILED_SAFE, S.ABANDONED}),
    S.RESCHEDULE_FLOW: frozenset(
        {S.AWAITING_APPROVAL, S.RESCHEDULE_FLOW, S.GATHERING, S.FAILED_SAFE, S.ABANDONED}
    ),
    S.PROACTIVE_REPLACEMENT: frozenset({S.AWAITING_APPROVAL, S.ABANDONED, S.FAILED_SAFE}),
    S.COMPLETED: frozenset({S.IDLE}),
    S.FAILED_SAFE: frozenset({S.IDLE}),
    S.ABANDONED: frozenset({S.IDLE}),
}

Trigger = Literal["user_message", "approval_grant", "tool_result", "system", "proactive_event"]
WorkflowKind = Literal["booking", "cancel", "reschedule", "proactive"]


class Candidate(BaseModel):
    provider_id: str
    name: str
    price_paise: int
    rating: float
    slot: datetime
    other_slots: list[datetime] = Field(default_factory=list)
    score: float
    reasons: list[str]
    injection_flagged: bool = False


class HoldInfo(BaseModel):
    hold_id: str
    provider_id: str
    provider_name: str
    slot: datetime
    expires_at: datetime
    quoted_price_paise: int
    search_price_paise: int


class TargetBooking(BaseModel):
    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: datetime
    price_paise: int


class Counters(BaseModel):
    turns: int = 0
    tool_calls: int = 0
    model_calls: int = 0


class WorkflowState(BaseModel):
    session_id: str
    user_id: str
    status: State = S.IDLE
    kind: WorkflowKind | None = None
    request: ServiceRequest = Field(default_factory=ServiceRequest)
    sources: dict[str, FieldSource] = Field(default_factory=dict)
    candidates: list[Candidate] = Field(default_factory=list)
    selected: Candidate | None = None
    hold: HoldInfo | None = None
    pending_approval: ApprovalRequest | None = None
    intent_id: str | None = None
    target_booking: TargetBooking | None = None
    new_slot: datetime | None = None
    alternatives: list[datetime] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    replaces_booking_id: str | None = None
    asked_fields: list[str] = Field(default_factory=list)
    history_summary: str = ""
    summary_upto_turn_id: int = 0
    counters: Counters = Field(default_factory=Counters)
    # Optimistic-concurrency token for the persisted session row.
    version: int = 0

    def can_transition(self, to: State) -> bool:
        return to in TRANSITIONS[self.status]

    def transition(self, to: State, trigger: Trigger) -> tuple[State, State]:
        """Move to `to` or raise. Returns (from, to) for tracing. Callers persist afterwards."""
        if not self.can_transition(to):
            raise IllegalTransition(f"{self.status} -> {to} is not allowed")
        if to in USER_RESERVED and trigger not in ("user_message", "approval_grant"):
            raise IllegalTransition(f"{to} requires user intent, got trigger={trigger}")
        before = self.status
        self.status = to
        return before, to

    def reset_workflow(self) -> None:
        """Start a fresh workflow in the same session; conversation summary is kept."""
        fresh = WorkflowState(
            session_id=self.session_id,
            user_id=self.user_id,
            history_summary=self.history_summary,
            summary_upto_turn_id=self.summary_upto_turn_id,
            counters=self.counters,
            version=self.version,
        )
        for name in WorkflowState.model_fields:
            setattr(self, name, getattr(fresh, name))
