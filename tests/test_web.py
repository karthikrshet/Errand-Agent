from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from errand.app import build_world
from errand.clock import FakeClock
from errand.config import Settings
from errand.llm.scripted import ScriptedClient
from errand.tools.scenarios import get_scenario
from errand.web.app import create_app
from tests.test_agent_flow import PLUMBER, SCRIPT


def _client(tmp_path: Path) -> TestClient:
    world = build_world(
        settings=Settings(),
        llm=ScriptedClient.from_map(SCRIPT),
        scenario=get_scenario("happy_path"),
        clock=FakeClock(),
        db_path=tmp_path / "a.db",
        ledger_path=tmp_path / "l.db",
    )
    return TestClient(create_app(world))


def test_chat_flow_over_http(tmp_path: Path) -> None:
    c = _client(tmp_path)
    assert c.get("/health").json() == {"status": "ok"}
    assert "Errand Agent" in c.get("/").text
    r = c.post("/api/sessions/w/message", json={"text": PLUMBER}).json()
    assert r["status"] == "PRESENTING_OPTIONS"
    r = c.post("/api/sessions/w/message", json={"text": "book option 1"}).json()
    assert r["approval"]["action"] == "book"
    r = c.post("/api/sessions/w/message", json={"text": f"CONFIRM {r['approval']['code']}"}).json()
    assert r["status"] == "COMPLETED"
    assert any("Booked and verified" in m for m in r["messages"])


def test_provider_cancel_and_watcher(tmp_path: Path) -> None:
    c = _client(tmp_path)
    c.post("/api/demo/provider-cancel", json={"booking_id": "b_up_ac01"})
    r = c.post("/api/sessions/w/watch").json()
    assert r["status"] == "AWAITING_APPROVAL"
    assert any("cancelled your booking" in m for m in r["messages"])


def test_demo_script_keys_parse() -> None:
    from datetime import date  # noqa: PLC0415

    from errand.agent.nlu import NLUResult  # noqa: PLC0415
    from errand.web.demo_script import demo_script  # noqa: PLC0415

    for raw in demo_script(date(2026, 11, 3)).values():
        NLUResult.model_validate_json(raw)
