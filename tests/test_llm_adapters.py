"""Offline tests for the Anthropic adapter (fake SDK client) and cassette record/replay."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from errand.config import Settings
from errand.llm.anthropic_client import FALLBACK_BETA, AnthropicClient
from errand.llm.base import LLMError, LLMRequest, Message
from errand.llm.cassette import CassetteClient, RecordingClient
from errand.llm.script import nlu
from errand.llm.scripted import ScriptedClient

REQ = LLMRequest(
    purpose="nlu",
    system="sys",
    messages=[Message(role="user", content="hi")],
    json_schema={"type": "object"},
    metadata={"user_text": "hi"},
)


def _response(text: str, stop: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )


def _bad_request(
    message: str = "output_config.format: schema not supported",
) -> anthropic.BadRequestError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        message, response=httpx2.Response(400, request=request), body=None
    )


class FakeSDK:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _client(outcomes: list[Any]) -> tuple[AnthropicClient, FakeSDK]:
    sdk = FakeSDK(outcomes)
    return AnthropicClient(Settings(), client=sdk), sdk  # type: ignore[arg-type]


def test_structured_request_shape_and_usage() -> None:
    client, sdk = _client([_response('{"intent": "status"}')])
    out = client.complete(REQ)
    assert out.text == '{"intent": "status"}'
    assert out.provider_input_tokens == 120
    call = sdk.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert call["output_config"]["effort"] == "low"
    assert call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert "thinking" not in call  # Opus 5.5: thinking can't be disabled; effort controls depth


def test_rejected_schema_falls_back_to_prompt_schema_once() -> None:
    client, sdk = _client([_bad_request(), _response("{}"), _response("{}")])
    client.complete(REQ)
    client.complete(REQ)
    assert "format" not in sdk.calls[1]["output_config"]
    assert "Return JSON matching this schema" in sdk.calls[1]["system"]
    assert "format" not in sdk.calls[2]["output_config"]  # remembered


def test_refusal_and_provider_errors_become_llm_errors() -> None:
    client, _ = _client([_response("", stop="refusal")])
    with pytest.raises(LLMError, match="refusal"):
        client.complete(REQ)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client, _ = _client([anthropic.APIConnectionError(request=request)])
    with pytest.raises(LLMError):
        client.complete(REQ)


def test_cassette_records_and_replays(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    live = ScriptedClient.from_map({"hi": nlu("status")})
    recorder = RecordingClient(live, path)
    recorder.complete(REQ)
    replay = CassetteClient(path)
    assert replay.complete(REQ).text == nlu("status")
    assert replay.complete(REQ).text == nlu("status")  # last answer repeats
    miss = REQ.model_copy(update={"metadata": {"user_text": "other"}})
    with pytest.raises(LLMError, match="no entry"):
        replay.complete(miss)


def test_non_schema_400_is_not_mistaken_for_schema_rejection() -> None:
    """Found on the first live run: a workspace error disabled structured output."""
    workspace_error = _bad_request(
        "This API key is not scoped to a workspace, so this request must include the "
        "anthropic-workspace-id header"
    )
    client, sdk = _client([workspace_error])
    with pytest.raises(LLMError, match="workspace"):
        client.complete(REQ)
    assert len(sdk.calls) == 1  # no silent retry without the schema
    assert client._schema_supported is True


def test_workspace_id_is_sent_as_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = AnthropicClient(Settings())
    assert client.client.default_headers.get("anthropic-workspace-id") == "wrkspc_test"


def test_missing_api_key_is_an_llm_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no key the SDK raises a bare TypeError; the agent must see LLMError and degrade."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    client = AnthropicClient(Settings())
    with pytest.raises(LLMError, match="no ANTHROPIC_API_KEY"):
        client.complete(
            LLMRequest(purpose="nlu", system="s", messages=[Message(role="user", content="hi")])
        )
