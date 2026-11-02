"""ToolGateway: the only way the agent touches provider tools.

Responsibilities, in order, for every attempt:
1. per-state allowlist (policy) and run budget (tool-call cap, wall clock);
2. the call itself over the JSON wire;
3. response validation against the fixed contract (malformed => typed error, never `Any`);
4. retry of *retryable* failures with capped exponential backoff and seeded jitter;
5. a `tool.call` trace event per attempt with redacted args and a sanitised, truncated result.

`book` timeouts are deliberately *not* retried here: the booking module must consult
`get_booking_status` first, because the booking may already exist.
"""

from __future__ import annotations

import json
import random
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

from errand.agent.limits import RunBudget
from errand.agent.policy import check_allowed
from errand.agent.state import State
from errand.agent.untrusted import sanitize
from errand.clock import Clock
from errand.config import RetryPolicy, Settings
from errand.errors import (
    ErrandError,
    MalformedToolResponse,
    ToolError,
    ToolTimeout,
    TransientToolError,
)
from errand.observability.trace import Tracer

T = TypeVar("T", bound=BaseModel)

# Timeouts on these are safe to retry blindly: reads have no effect, cancel/reschedule are keyed.
RETRY_ON_TIMEOUT = frozenset(
    {"search_providers", "get_provider", "list_bookings", "get_booking_status", "cancel",
     "reschedule"}
)  # fmt: skip
TRACE_RESULT_CHARS = 1200


class Transport(Protocol):
    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class UnexpectedToolError(ToolError):
    """The provider failed in a way the contract does not describe."""


class RetriesExhausted(ToolError):
    def __init__(self, tool: str, attempts: int, last: Exception) -> None:
        super().__init__(f"{tool} failed after {attempts} attempts: {last}")
        self.tool = tool
        self.attempts = attempts
        self.last = last


class ToolGateway:
    def __init__(
        self,
        transport: Transport,
        *,
        clock: Clock,
        settings: Settings,
        tracer: Tracer,
        rng: random.Random,
        budget: RunBudget,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.settings = settings
        self.tracer = tracer
        self.rng = rng
        self.budget = budget

    def call(self, tool: str, args: BaseModel, out: type[T], *, state: State) -> T:
        check_allowed(state, tool)
        policy = self.settings.retry_for(tool)
        payload = args.model_dump(mode="json", exclude_none=True)
        last: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1):
            self.budget.charge_tool()
            try:
                return self._attempt(tool, payload, out, attempt, state)
            except (TransientToolError, ToolTimeout) as exc:
                last = exc
                if isinstance(exc, ToolTimeout) and tool not in RETRY_ON_TIMEOUT:
                    raise
                if attempt < policy.max_attempts:
                    self.clock.sleep(self._backoff(policy, attempt))
        assert last is not None
        self.tracer.emit("tool.exhausted", tool=tool, attempts=policy.max_attempts)
        raise RetriesExhausted(tool, policy.max_attempts, last)

    def _attempt(
        self, tool: str, payload: dict[str, Any], out: type[T], attempt: int, state: State
    ) -> T:
        with self.tracer.span(
            "tool.call", tool=tool, args=_json(payload), attempt=attempt, state=state.value
        ) as span:
            try:
                raw = self.transport.call(tool, payload)
            except ErrandError as exc:
                span.set(outcome="error", error=type(exc).__name__, message=str(exc))
                raise
            except Exception as exc:
                span.set(outcome="error", error="UnexpectedToolError", message=repr(exc))
                raise UnexpectedToolError(f"{tool}: {exc!r}") from exc
            try:
                result = out.model_validate(raw)
            except ValidationError as exc:
                span.set(outcome="malformed", error="MalformedToolResponse")
                raise MalformedToolResponse(f"{tool} returned an invalid payload") from exc
            span.set(outcome="ok", result=_sanitised_result(raw))
            return result

    def _backoff(self, policy: RetryPolicy, attempt: int) -> float:
        base = min(policy.max_delay_s, policy.base_delay_s * 2.0 ** (attempt - 1))
        jitter = 1 + policy.jitter_ratio * (self.rng.random() * 2 - 1)
        return round(base * jitter, 4)


def _json(value: dict[str, Any]) -> JsonValue:
    loaded: JsonValue = json.loads(json.dumps(value, default=str))
    return loaded


def _sanitised_result(raw: dict[str, Any]) -> JsonValue:
    """Strip invisible characters and truncate for the trace; untrusted text stays inert data."""
    text = sanitize(json.dumps(raw, ensure_ascii=False), max_chars=TRACE_RESULT_CHARS).text
    return text
