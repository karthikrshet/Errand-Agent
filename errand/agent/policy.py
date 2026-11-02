"""Which tools may run in which workflow state.

This is the second wall behind approvals: even if a model (or an injected review) talked the
orchestrator into *trying* `book` while presenting options, the gateway refuses the call.
Side-effect tools are only reachable from EXECUTING/VERIFYING, which are only reachable via a
user-derived grant.
"""

from __future__ import annotations

from errand.agent.state import State
from errand.errors import ErrandError

READS = frozenset({"search_providers", "get_provider", "list_bookings", "get_booking_status"})
S = State

ALLOWED: dict[State, frozenset[str]] = {
    S.IDLE: READS,
    S.GATHERING: READS,
    S.SEARCHING: READS,
    S.PRESENTING_OPTIONS: READS,
    S.HOLDING: READS | {"hold_slot"},
    S.AWAITING_APPROVAL: READS,
    S.EXECUTING: READS | {"book", "cancel", "reschedule"},
    S.VERIFYING: READS | {"book"},
    S.CANCEL_FLOW: READS,
    S.RESCHEDULE_FLOW: READS,
    S.PROACTIVE_REPLACEMENT: READS | {"hold_slot"},
    S.COMPLETED: READS,
    S.FAILED_SAFE: READS,
    S.ABANDONED: READS,
}


class PolicyViolation(ErrandError):
    pass


def check_allowed(state: State, tool: str) -> None:
    if tool not in ALLOWED[state]:
        raise PolicyViolation(f"tool {tool!r} is not allowed in state {state}")
