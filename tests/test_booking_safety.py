"""Safety properties of approvals and booking, exercised through the real stack."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from errand.agent.approval import idempotency_key_for
from errand.agent.crash import CRASH_POINTS, CrashHooks, CrashPoint, SimulatedCrash
from errand.agent.state import State
from errand.agent.store import AgentStore
from errand.app import World, build_world
from errand.clock import FakeClock
from errand.config import Settings
from errand.llm.scripted import ScriptedClient
from errand.tools.scenarios import Scenario, get_scenario
from tests.test_agent_flow import PLUMBER, SCRIPT


def _world(
    tmp_path: Path,
    scenario: Scenario,
    clock: FakeClock | None = None,
    crash: CrashHooks | None = None,
) -> World:
    return build_world(
        settings=Settings(),
        llm=ScriptedClient.from_map(SCRIPT),
        scenario=scenario,
        clock=clock or FakeClock(),
        db_path=tmp_path / "agent.db",
        ledger_path=tmp_path / "ledger.db",
        crash=crash,
    )


def _to_approval(w: World, session: str = "s1") -> None:
    w.agent.handle_message(session, PLUMBER)
    r = w.agent.handle_message(session, "book option 1")
    assert r.state.status == State.AWAITING_APPROVAL


def _new_bookings(w: World) -> int:
    return w.env.count_bookings() - 6  # seeded history for u_demo


def test_repeated_yes_books_exactly_once(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    for _ in range(3):
        w.agent.handle_message("s1", "yes yes yes")
    assert _new_bookings(w) == 1
    assert len(w.tracer.of_kind("approval.consumed")) == 1


def test_concurrent_grant_consumption_has_one_winner(tmp_path: Path) -> None:
    """The DB alone (no session lease) lets exactly one of 8 racing confirmations through."""
    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    req = w.agent.load("s1").pending_approval
    assert req is not None
    results: list[object] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        store = AgentStore(tmp_path / "agent.db")  # own connection, like another process
        barrier.wait()
        results.append(
            store.grant_and_open_intent(
                req,
                session_id="s1",
                source_turn_id=1,
                now=w.clock.now(),
                grant_id=f"g_{i}",
                intent_id=f"i_{i}",
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([x for x in results if x is not None]) == 1
    assert len(w.store.all_intents()) == 1


def test_concurrent_double_confirmation_is_stable(tmp_path: Path) -> None:
    """Regression for BUGS.md #4: two processes confirming at once, many times over."""
    from evals.harness import execute  # noqa: PLC0415
    from evals.invariants import check_all  # noqa: PLC0415
    from evals.scenarios import all_scenarios  # noqa: PLC0415

    sc = next(s for s in all_scenarios() if s.sid == "27")
    for i in range(15):
        run = execute(sc, tmp_path / str(i))
        failures = sc.check(run) + [m for v in check_all(run).values() for m in v]
        assert not failures, (i, failures)


def test_grant_is_refused_if_hold_changed_after_approval(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    st = w.agent.load("s1")
    assert st.hold is not None
    other = st.candidates[0].other_slots[0]
    st.hold = st.hold.model_copy(update={"slot": other})
    w.store.save_state(st, w.clock.now())
    r = w.agent.handle_message("s1", "yes")
    assert "no longer applies" in r.text
    assert w.tracer.of_kind("approval.rejected_at_execution")
    assert "book" not in [e.data["tool"] for e in w.tracer.of_kind("tool.call")]
    assert _new_bookings(w) == 0


def test_price_change_at_hold_is_shown_as_diff(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("price_change"))
    w.agent.handle_message("s1", PLUMBER)
    r = w.agent.handle_message("s1", "book option 1")
    assert "Price changed from" in r.text
    req = r.state.pending_approval
    assert req is not None
    assert req.price_paise == r.state.candidates[0].price_paise + 25000


def test_hold_expiry_rehold_requires_fresh_approval(tmp_path: Path) -> None:
    clock = FakeClock()
    w = _world(tmp_path, Scenario(book_outcomes=["success", "success"]), clock=clock)
    _to_approval(w)
    first = w.agent.load("s1").pending_approval
    assert first is not None
    clock.advance(601)
    r = w.agent.handle_message("s1", "yes")
    assert r.state.status == State.AWAITING_APPROVAL
    assert "hold expired" in r.text.lower()
    second = r.state.pending_approval
    assert second is not None
    assert second.hold_id != first.hold_id
    assert second.fingerprint != first.fingerprint
    assert _new_bookings(w) == 0
    r = w.agent.handle_message("s1", "yes")
    assert r.state.status == State.COMPLETED
    assert _new_bookings(w) == 1


def test_ambiguous_reply_never_grants_and_reissues(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    before = w.agent.load("s1").pending_approval
    assert before is not None
    r = w.agent.handle_message("s1", "yes but make it 11am")
    after = r.state.pending_approval
    assert after is not None
    assert after.confirm_code != before.confirm_code
    assert w.tracer.of_kind("approval.granted") == []
    assert w.store.approval_status(before.request_id) == "invalidated"
    # the old code no longer works
    r = w.agent.handle_message("s1", f"CONFIRM {before.confirm_code}")
    assert "doesn't match" in r.text
    assert _new_bookings(w) == 0


def test_no_reply_rejects(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    r = w.agent.handle_message("s1", "no")
    assert r.state.status == State.PRESENTING_OPTIONS
    assert r.state.pending_approval is None
    assert _new_bookings(w) == 0


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_crash_matrix_reconciles_to_one_outcome(tmp_path: Path, point: CrashPoint) -> None:
    clock = FakeClock()
    w = _world(tmp_path, Scenario(book_outcomes=["success", "success"]), clock, CrashHooks({point}))
    _to_approval(w)
    with pytest.raises(SimulatedCrash):
        w.agent.handle_message("s1", "yes")

    restarted = _world(tmp_path, Scenario(book_outcomes=["success"]), clock)
    r = restarted.agent.startup("s1")
    intents = restarted.store.all_intents()
    assert len(intents) == 1
    assert intents[0].status != "PENDING"
    if point == "after_intent_write":
        assert "Nothing was booked" in r.text
        assert r.state.status == State.AWAITING_APPROVAL
        r = restarted.agent.handle_message("s1", "yes")
    assert r.state.status == State.COMPLETED
    assert "verified" in r.text.lower()
    assert _new_bookings(restarted) == 1
    key = idempotency_key_for("u_demo", intents[0].approval_fingerprint)
    assert restarted.env.count_bookings(key) <= 1


def test_stale_session_write_is_rejected(tmp_path: Path) -> None:
    """Regression for BUGS.md #2: a stale snapshot must not overwrite a newer session state."""
    from errand.agent.store import StaleState  # noqa: PLC0415

    w = _world(tmp_path, get_scenario("happy_path"))
    _to_approval(w)
    a, b = w.agent.load("s1"), w.agent.load("s1")
    a.status = State.COMPLETED
    w.store.save_state(a, w.clock.now())
    b.status = State.AWAITING_APPROVAL
    with pytest.raises(StaleState):
        w.store.save_state(b, w.clock.now())
    assert w.agent.load("s1").status == State.COMPLETED


def test_restart_reverifies_a_succeeded_intent_before_claiming(tmp_path: Path) -> None:
    """Regression for BUGS.md #3: after a restart, success is re-checked at the source of truth."""
    clock = FakeClock()
    crash = CrashHooks({"after_booking_before_state_save"})
    w = _world(tmp_path, Scenario(book_outcomes=["success"]), clock, crash)
    _to_approval(w)
    with pytest.raises(SimulatedCrash):
        w.agent.handle_message("s1", "yes")
    restarted = _world(tmp_path, Scenario(), clock)
    r = restarted.agent.startup("s1")
    tools = [e.data["tool"] for e in restarted.tracer.of_kind("tool.call")]
    assert tools == ["get_booking_status"]
    verified = restarted.tracer.of_kind("booking.verified")
    assert verified
    assert verified[0].data["via"] == "restart_check"
    assert "verified" in r.text
