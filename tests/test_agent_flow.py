"""End-to-end flows through the real orchestrator, scripted model, mock world and stores."""

from __future__ import annotations

from pathlib import Path

from errand.agent.state import State
from errand.app import World, build_world
from errand.clock import FakeClock
from errand.config import Settings
from errand.llm.script import nlu, select
from errand.llm.scripted import ScriptedClient
from errand.tools.scenarios import Scenario, get_scenario

PLUMBER = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500. Indiranagar."
PLUMBER_NLU = nlu(
    "request",
    service="plumber",
    job="kitchen tap",
    area="Indiranagar",
    date_start="2026-11-02",
    date_end="2026-11-08",
    earliest_hour=8,
    latest_hour=12,
    budget_rupees=1500,
)
SCRIPT = {PLUMBER: PLUMBER_NLU, "book option 1": select(1), "book option 2": select(2)}


def _world(tmp_path: Path, scenario: Scenario, script: dict[str, str] | None = None) -> World:
    return build_world(
        settings=Settings(),
        llm=ScriptedClient.from_map(script or SCRIPT),
        scenario=scenario,
        clock=FakeClock(),
        db_path=tmp_path / "agent.db",
        ledger_path=tmp_path / "ledger.db",
    )


def test_happy_path_books_once_and_verifies(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("happy_path"))
    r1 = w.agent.handle_message("s1", PLUMBER)
    assert r1.state.status == State.PRESENTING_OPTIONS
    assert "1." in r1.text
    assert all(c.price_paise <= 150000 for c in r1.state.candidates)
    r2 = w.agent.handle_message("s1", "book option 1")
    assert r2.state.status == State.AWAITING_APPROVAL
    assert "Please confirm: BOOK" in r2.text
    r3 = w.agent.handle_message("s1", "yes")
    assert r3.state.status == State.COMPLETED
    assert "Booked and verified" in r3.text
    assert w.env.count_bookings() == 7  # 6 seeded for u_demo + 1 new
    kinds = [e.kind for e in w.tracer.events]
    assert kinds.index("approval.granted") < kinds.index("approval.consumed")


def test_timeout_after_success_checks_status_and_does_not_rebook(tmp_path: Path) -> None:
    w = _world(tmp_path, get_scenario("timeout_after_success"))
    w.agent.handle_message("s1", PLUMBER)
    w.agent.handle_message("s1", "book option 1")
    r = w.agent.handle_message("s1", "yes")
    assert r.state.status == State.COMPLETED
    tools = [e.data["tool"] for e in w.tracer.of_kind("tool.call")]
    book_idx = tools.index("book")
    assert tools[book_idx + 1] == "get_booking_status"
    assert tools.count("book") == 1


def test_injected_reviews_are_flagged_and_change_nothing(tmp_path: Path) -> None:
    script = {
        **SCRIPT,
        PLUMBER: nlu(
            "request",
            service="plumber",
            area="Indiranagar",
            date_start="2026-11-02",
            date_end="2026-11-08",
        ),
    }
    w = _world(tmp_path, get_scenario("happy_path"), script)
    r = w.agent.handle_message("s1", PLUMBER)
    assert r.state.status == State.PRESENTING_OPTIONS
    assert w.tracer.of_kind("security.injection_detected")
    assert "[!]" in r.text
    assert w.tracer.of_kind("approval.granted") == []
    assert all("SYSTEM" not in c.name for c in r.state.candidates)
