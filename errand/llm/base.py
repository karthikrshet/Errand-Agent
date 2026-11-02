"""Provider-neutral LLM interface. The agent depends only on this protocol."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field, JsonValue

Purpose = Literal["nlu", "review_summary", "history_summary"]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    purpose: Purpose
    system: str
    messages: list[Message]
    json_schema: dict[str, JsonValue] | None = None
    max_tokens: int = 800
    # Never sent to a provider. Lets scripted/recorded clients key responses by the turn.
    metadata: dict[str, str] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    provider_input_tokens: int | None = None


class LLMError(Exception):
    """Provider error, timeout, or rate limit. Retried a bounded number of times by callers."""


class LLMClient(Protocol):
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
