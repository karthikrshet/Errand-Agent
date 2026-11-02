"""Anthropic adapter. Completed in Phase 7; present now so the CLI wiring is final."""

from __future__ import annotations

from errand.config import Settings
from errand.llm.base import LLMError, LLMRequest, LLMResponse


class AnthropicClient:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.model

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError("Anthropic adapter not configured yet")
