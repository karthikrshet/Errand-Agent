from __future__ import annotations

import random
from pathlib import Path

import pytest

from errand.agent.limits import RunBudget
from errand.agent.policy import PolicyViolation
from errand.agent.state import State
from errand.agent.untrusted import sanitize
from errand.clock import FakeClock
from errand.config import Settings
from errand.errors import LimitExceeded, MalformedToolResponse, ToolTimeout
from errand.observability.redact import redact_text
from errand.observability.trace import Tracer, estimate_cost_usd, load_trace
from errand.tools import catalog
from errand.tools import contracts as c
from errand.tools.gateway import RetriesExhausted, ToolGateway, UnexpectedToolError
from errand.tools.scenarios import Scenario
from tests.conftest import EnvFactory


def _gateway(
    env_factory: EnvFactory,
    clock: FakeClock,
    scenario: Scenario | None = None,
    settings: Settings | None = None,
    path: Path | None = None,
) -> ToolGateway:
    settings = settings or Settings()
    return ToolGateway(
        env_factory(scenario),
        clock=clock,
        settings=settings,
        tracer=Tracer(clock, "run-test", path),
        rng=random.Random(0),
        budget=RunBudget(settings, clock),
    )


GET = c.GetProviderInput(provider_id="pl_aqua")


def test_gateway_returns_typed_output_and_traces(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock)
    out = gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)
    assert out.provider.provider_id == "pl_aqua"
    [event] = gw.tracer.of_kind("tool.call")
    assert event.data["outcome"] == "ok"
    assert event.data["args"] == {"provider_id": "pl_aqua"}
    assert "\u200b" not in str(event.data["result"])


def test_transient_errors_retry_with_backoff(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock, Scenario(injected_failures={"get_provider": ["503", "timeout"]}))
    gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)
    attempts = [e.data["outcome"] for e in gw.tracer.of_kind("tool.call")]
    assert attempts == ["error", "error", "ok"]
    assert len(clock.slept) == 2
    assert clock.slept[1] > clock.slept[0]


def test_retries_exhaust_cleanly(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock, Scenario(injected_failures={"get_provider": ["503"] * 5}))
    with pytest.raises(RetriesExhausted):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)
    assert gw.tracer.of_kind("tool.exhausted")


def test_book_timeout_is_not_retried_by_gateway(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock, Scenario(injected_failures={"book": ["timeout"]}))
    args = c.BookInput(hold_id="h_1", idempotency_key="k")
    with pytest.raises(ToolTimeout):
        gw.call("book", args, c.BookOutput, state=State.EXECUTING)
    assert len(gw.tracer.of_kind("tool.call")) == 1


def test_policy_blocks_side_effects_outside_executing(
    make_env: EnvFactory, clock: FakeClock
) -> None:
    gw = _gateway(make_env, clock)
    args = c.BookInput(hold_id="h_1", idempotency_key="k")
    for state in (State.PRESENTING_OPTIONS, State.AWAITING_APPROVAL, State.IDLE):
        with pytest.raises(PolicyViolation):
            gw.call("book", args, c.BookOutput, state=state)
    assert gw.tracer.of_kind("tool.call") == []


def test_malformed_and_crash_are_typed(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock, Scenario(injected_failures={"get_provider": ["malformed"]}))
    with pytest.raises(MalformedToolResponse):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)
    gw = _gateway(make_env, clock, Scenario(injected_failures={"get_provider": ["crash"]}))
    with pytest.raises(UnexpectedToolError):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)


def test_tool_call_cap(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock, settings=Settings(max_tool_calls_per_run=2))
    for _ in range(2):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)
    with pytest.raises(LimitExceeded):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)


def test_wall_clock_cap(make_env: EnvFactory, clock: FakeClock) -> None:
    gw = _gateway(make_env, clock)
    clock.advance(61)
    with pytest.raises(LimitExceeded, match="wall_clock"):
        gw.call("get_provider", GET, c.GetProviderOutput, state=State.SEARCHING)


# ---------------------------------------------------------------- untrusted text


@pytest.mark.parametrize(
    "text",
    [
        catalog.INJECTION_SYSTEM_OVERRIDE,
        catalog.INJECTION_ROLE_SPOOF,
        catalog.INJECTION_ZERO_WIDTH,
        catalog.INJECTION_FAKE_PRICE,
        catalog.INJECTED_PROVIDER_NAME,
    ],
)
def test_every_injection_fixture_is_flagged(text: str) -> None:
    result = sanitize(text)
    assert result.injection_suspected, text
    assert "\u200b" not in result.text


def test_benign_reviews_are_not_flagged() -> None:
    for p in catalog.CATALOG:
        for r in p.reviews:
            if r.text not in {
                catalog.INJECTION_SYSTEM_OVERRIDE,
                catalog.INJECTION_ROLE_SPOOF,
                catalog.INJECTION_ZERO_WIDTH,
                catalog.INJECTION_FAKE_PRICE,
            }:
                assert not sanitize(r.text).flags, r.text


def test_sanitize_truncates() -> None:
    out = sanitize("x" * 600, max_chars=500)
    assert out.truncated
    assert len(out.text) == 501


# ---------------------------------------------------------------- trace / redaction


def test_redaction() -> None:
    text = "call me on +91 98765 43210 or 9876543210, mail a.b@example.com"
    assert redact_text(text) == "call me on [PHONE] or [PHONE], mail [EMAIL]"


def test_trace_writes_jsonl_and_redacts(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "t.jsonl"
    tracer = Tracer(clock, "r1", path)
    tracer.emit("run.start", note="user is x@y.com")
    tracer.emit("model.call", input_tokens=10, output_tokens=5, cost_usd=0.001)
    tracer.close()
    events = load_trace(path)
    assert [e.kind for e in events] == ["run.start", "model.call"]
    assert events[0].data["note"] == "user is [EMAIL]"
    assert tracer.totals.input_tokens == 10


def test_cost_estimate() -> None:
    from errand.config import ModelPrice  # noqa: PLC0415

    prices = {"m": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)}
    assert estimate_cost_usd("m", 1_000_000, 0, prices) == 3.0
    assert estimate_cost_usd("unknown", 1, 1, prices) is None
