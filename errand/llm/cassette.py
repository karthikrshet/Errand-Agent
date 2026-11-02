"""Record real model output once, replay it offline forever.

`RecordingClient` wraps a live client and appends every (purpose, user text, response) to a
JSONL cassette. `CassetteClient` replays a cassette deterministically, keyed by purpose and the
user's message, so selected eval scenarios run against *real* model behaviour with no network.
A missing entry raises `LLMError`, exercising the same degraded path as a provider outage.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

from errand.llm.base import LLMClient, LLMError, LLMRequest, LLMResponse


def _key(request: LLMRequest) -> str:
    ref = request.metadata.get("user_text") or request.metadata.get("provider_id", "")
    return f"{request.purpose}::{ref}"


class RecordingClient:
    def __init__(self, inner: LLMClient, path: Path) -> None:
        self.inner = inner
        self.model = inner.model
        self.path = path
        self.requests: list[LLMRequest] = []
        path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = self.inner.complete(request)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": _key(request), "response": response.model_dump()}) + "\n")
        return response


class CassetteClient:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, deque[LLMResponse]] = defaultdict(deque)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                self._entries[row["key"]].append(LLMResponse.model_validate(row["response"]))
        first = next((q[0] for q in self._entries.values() if q), None)
        self.model = first.model if first else "cassette"
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        queue = self._entries.get(_key(request))
        if not queue:
            raise LLMError(f"cassette {self.path.name} has no entry for {_key(request)!r}")
        # Replay in order; the last answer for a key repeats (retries see the same output).
        return queue.popleft() if len(queue) > 1 else queue[0]
