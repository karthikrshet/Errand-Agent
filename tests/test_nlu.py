from __future__ import annotations

from datetime import date

from errand.agent.limits import RunBudget
from errand.agent.nlu import NLU, NLUResult, RescheduleTo
from errand.clock import FakeClock
from errand.config import Settings
from errand.llm.base import LLMError, LLMRequest
from errand.llm.script import nlu
from errand.llm.scripted import ScriptedClient, failing
from errand.observability.trace import Tracer


def _nlu(client: ScriptedClient, settings: Settings | None = None) -> tuple[NLU, Tracer]:
    settings = settings or Settings()
    clock = FakeClock()
    tracer = Tracer(clock, "t")
    return NLU(client, settings, tracer, RunBudget(settings, clock)), tracer


def test_reschedule_to_parses_date_field() -> None:
    """Regression for BUGS.md #1 (field named `date` shadowing its type)."""
    assert RescheduleTo.model_validate({"date": "2026-11-07"}).date == date(2026, 11, 7)


def test_valid_output_is_parsed_and_traced_with_manifest() -> None:
    client = ScriptedClient.from_map({"hi": nlu("request", service="plumber")})
    engine, tracer = _nlu(client)
    out = engine.interpret("hi", "sys", "ctx", {"total_tokens": 10})
    assert isinstance(out, NLUResult)
    assert out.patch.service == "plumber"
    [call] = tracer.of_kind("model.call")
    assert call.data["context_manifest"] == {"total_tokens": 10}


def test_fenced_json_is_accepted() -> None:
    client = ScriptedClient.from_map({"hi": "```json\n" + nlu("status") + "\n```"})
    out, _ = _nlu(client)[0].interpret("hi", "sys", "hi", {}), None
    assert out is not None
    assert out.intent == "status"


def test_invalid_output_gets_one_repair_then_succeeds() -> None:
    replies = iter(['{"intent": "teleport"}', nlu("status")])
    client = ScriptedClient(lambda _req: next(replies))
    engine, tracer = _nlu(client)
    out = engine.interpret("hi", "sys", "hi", {})
    assert out is not None
    assert out.intent == "status"
    assert tracer.of_kind("llm.repair")
    repair_prompt = client.requests[1].messages[-1].content
    assert "failed validation" in repair_prompt


def test_invalid_twice_falls_back() -> None:
    engine, tracer = _nlu(ScriptedClient(lambda _req: "not json"))
    assert engine.interpret("hi", "sys", "hi", {}) is None
    assert tracer.of_kind("llm.fallback")[0].data["reason"] == "invalid_output"


def test_provider_errors_retry_then_fall_back() -> None:
    client = ScriptedClient(failing())
    engine, tracer = _nlu(client)
    assert engine.interpret("hi", "sys", "hi", {}) is None
    assert len(client.requests) == 3  # 1 + llm_max_retries(2)
    assert tracer.of_kind("llm.fallback")[0].data["reason"] == "provider_error"


def test_transient_error_then_success() -> None:
    calls = {"n": 0}

    def handler(_req: LLMRequest) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("rate limited")
        return nlu("status")

    engine, _ = _nlu(ScriptedClient(handler))
    out = engine.interpret("hi", "sys", "hi", {})
    assert out is not None
    assert out.intent == "status"
