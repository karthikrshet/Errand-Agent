"""Anthropic adapter (live runs). Evals never use it; they replay scripted or recorded output.

Design notes:
* Structured output via `output_config.format` (json_schema) so the model returns schema-shaped
  JSON. Our own pydantic validation + one repair round still runs on top: the adapter never
  trusts the provider to have validated anything.
* If the provider rejects our schema (some JSON-schema constructs are unsupported), we fall back
  once to a plain request with the schema in the prompt, and remember that for this client.
  Other 400s (auth, workspace, model access) are never mistaken for a schema problem.
* `ANTHROPIC_WORKSPACE_ID`, if set, is sent as `anthropic-workspace-id` (needed for user-level
  keys that are not scoped to a workspace).
* Server-side refusal fallbacks are enabled by default (`fallbacks: "default"`).
* Every provider failure is mapped to `LLMError`, which the NLU layer retries a bounded number
  of times before degrading to deterministic fallback handling.
* Usage: `input_tokens` is the provider-reported count; the NLU trace also records our tiktoken
  estimate, so tokenizer drift is visible per call.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from errand.config import Settings
from errand.llm.base import LLMError, LLMRequest, LLMResponse

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.model = settings.model
        self.settings = settings
        # User-level keys (not scoped to a workspace) must name one on every request.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self.client = client or anthropic.Anthropic(
            timeout=settings.llm_timeout_s,
            max_retries=0,  # retries are ours, bounded + traced
            default_headers=headers,
        )
        self._schema_supported = True

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return self._create(request, with_schema=self._schema_supported)
        except anthropic.BadRequestError as exc:
            if request.json_schema is not None and self._schema_supported and _about_schema(exc):
                self._schema_supported = False
                return self._wrap(lambda: self._create(request, with_schema=False))
            raise LLMError(f"bad request: {exc.message}") from exc
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    def _wrap(self, fn: Any) -> LLMResponse:
        try:
            result: LLMResponse = fn()
            return result
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    def _create(self, request: LLMRequest, *, with_schema: bool) -> LLMResponse:
        system = request.system
        kwargs: dict[str, Any] = {}
        output_config: dict[str, Any] = {"effort": self.settings.llm_effort}
        if request.json_schema is not None:
            if with_schema:
                output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
            else:
                system += "\n\nReturn JSON matching this schema:\n" + json.dumps(
                    request.json_schema
                )
        if self.settings.llm_server_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(request.max_tokens, 2048),
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "output_config": output_config,
            **kwargs,
        }
        response = self.client.beta.messages.create(**params)
        if response.stop_reason == "refusal":
            raise LLMError("model declined the request (refusal)")
        text = "".join(b.text for b in response.content if b.type == "text")
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            provider_input_tokens=response.usage.input_tokens,
        )


SCHEMA_ERROR_HINTS = ("schema", "output_config", "json_schema", "format")


def _about_schema(exc: anthropic.BadRequestError) -> bool:
    """Only a schema rejection justifies retrying without structured output; any other 400
    (auth, workspace, model access) must surface instead of silently degrading."""
    message = str(exc.message).lower()
    return any(hint in message for hint in SCHEMA_ERROR_HINTS)
