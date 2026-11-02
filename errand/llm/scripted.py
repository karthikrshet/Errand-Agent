"""Offline, deterministic LLM client used by every automated test and eval.

Responses come from a handler keyed on the request (normally `purpose` + the user's message).
A handler may raise `LLMError` to simulate an outage. Token usage is computed with the same
counter as the context budget so traces look like real runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from errand.llm.base import LLMError, LLMRequest, LLMResponse
from errand.llm.tokens import count_tokens

Handler = Callable[[LLMRequest], str]


class ScriptedClient:
    def __init__(self, handler: Handler, model: str = "scripted") -> None:
        self.handler = handler
        self.model = model
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self.handler(request)
        prompt = request.system + "".join(m.content for m in request.messages)
        return LLMResponse(
            text=text,
            model=self.model,
            input_tokens=count_tokens(prompt),
            output_tokens=count_tokens(text),
        )

    @classmethod
    def from_map(
        cls,
        responses: Mapping[str, str | Exception],
        *,
        default: str | None = None,
        purpose: str = "nlu",
    ) -> ScriptedClient:
        """Map the user's message text -> raw model output (or an exception to raise)."""

        def handler(request: LLMRequest) -> str:
            if request.purpose != purpose:
                raise LLMError(f"no scripted response for purpose={request.purpose}")
            key = request.metadata.get("user_text", "")
            value = responses.get(key, default)
            if value is None:
                raise LLMError(f"no scripted response for {key!r}")
            if isinstance(value, Exception):
                raise value
            return value

        return cls(handler)


def failing(message: str = "provider unavailable") -> Handler:
    def handler(_: LLMRequest) -> str:
        raise LLMError(message)

    return handler
