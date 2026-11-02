from __future__ import annotations

from datetime import date

import pytest

from errand.agent.state import TERMINAL, TRANSITIONS, State, WorkflowState
from errand.domain.request import ServiceRequest, SlotPatch, age_sources, merge_patch
from errand.errors import IllegalTransition


def _wf() -> WorkflowState:
    return WorkflowState(session_id="s1", user_id="u_demo")


def test_every_state_has_a_transition_row() -> None:
    assert set(TRANSITIONS) == set(State)


def test_terminal_states_only_reset_to_idle() -> None:
    for s in TERMINAL:
        assert TRANSITIONS[s] == frozenset({State.IDLE})


def test_legal_path_to_completion() -> None:
    wf = _wf()
    path = [
        (State.GATHERING, "user_message"),
        (State.SEARCHING, "user_message"),
        (State.PRESENTING_OPTIONS, "tool_result"),
        (State.HOLDING, "user_message"),
        (State.AWAITING_APPROVAL, "tool_result"),
        (State.EXECUTING, "approval_grant"),
        (State.VERIFYING, "tool_result"),
        (State.COMPLETED, "tool_result"),
    ]
    for to, trigger in path:
        wf.transition(to, trigger)  # type: ignore[arg-type]
    assert wf.status == State.COMPLETED


def test_illegal_transition_raises() -> None:
    wf = _wf()
    with pytest.raises(IllegalTransition):
        wf.transition(State.EXECUTING, "approval_grant")


@pytest.mark.parametrize("target", [State.EXECUTING, State.CANCEL_FLOW, State.RESCHEDULE_FLOW])
def test_user_reserved_states_reject_tool_triggers(target: State) -> None:
    wf = _wf()
    wf.status = State.AWAITING_APPROVAL if target == State.EXECUTING else State.IDLE
    with pytest.raises(IllegalTransition, match="requires user intent"):
        wf.transition(target, "tool_result")


def test_reset_keeps_session_identity_and_summary() -> None:
    wf = _wf()
    wf.history_summary = "earlier: booked electrician"
    wf.request.service = "plumber"
    wf.status = State.COMPLETED
    wf.reset_workflow()
    assert wf.status == State.IDLE
    assert wf.request.service is None
    assert wf.history_summary == "earlier: booked electrician"


def test_correction_changes_only_the_corrected_field() -> None:
    req = ServiceRequest(
        service="plumber",
        job="kitchen tap",
        area="Indiranagar",
        date_start=date(2026, 11, 6),
        date_end=date(2026, 11, 6),
        earliest_hour=8,
        latest_hour=12,
        budget_paise=150000,
    )
    sources = dict.fromkeys(req.model_dump(exclude_defaults=True), "user_earlier")
    out = merge_patch(
        req, sources, SlotPatch(date_start=date(2026, 11, 7), date_end=date(2026, 11, 7))
    )
    assert out.changed == ["date_start", "date_end"]
    after = out.request.model_dump()
    before = req.model_dump()
    for k in before:
        if k not in ("date_start", "date_end"):
            assert after[k] == before[k], k
    assert out.sources["date_start"] == "user_this_turn"
    assert out.sources["area"] == "user_earlier"


def test_budget_in_rupees_becomes_paise_and_blocklist_appends() -> None:
    out = merge_patch(
        ServiceRequest(blocked_provider_ids=["a"]),
        {},
        SlotPatch(budget_rupees=1500, block_provider_id="b"),
    )
    assert out.request.budget_paise == 150000
    assert out.request.blocked_provider_ids == ["a", "b"]


def test_moving_start_past_end_extends_end() -> None:
    req = ServiceRequest(date_start=date(2026, 11, 3), date_end=date(2026, 11, 4))
    out = merge_patch(req, {}, SlotPatch(date_start=date(2026, 11, 7)))
    assert out.request.date_end == date(2026, 11, 7)


def test_missing_reports_required_fields_only() -> None:
    assert ServiceRequest(service="plumber").missing() == ["area", "date_start"]


def test_age_sources() -> None:
    assert age_sources({"a": "user_this_turn", "b": "memory_explicit"}) == {
        "a": "user_earlier",
        "b": "memory_explicit",
    }
