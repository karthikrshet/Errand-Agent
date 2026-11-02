"""Structured run traces: one JSONL file per run, plus in-memory events for evals and the UI.

Events are append-only and flushed per line, so a crash mid-run still leaves a readable trace
up to the crash point (which is exactly when you want one).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, JsonValue

from errand.clock import Clock
from errand.config import ModelPrice
from errand.observability.redact import redact


class TraceEvent(BaseModel):
    seq: int
    ts: str
    run_id: str
    kind: str
    data: dict[str, JsonValue]


class Totals(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_latencies_ms: list[float] = []


Listener = Callable[[TraceEvent], None]


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, prices: dict[str, ModelPrice]
) -> float | None:
    price = prices.get(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1e6


class Span:
    """Mutable bag of fields that a `with tracer.span(...)` block fills in before emitting."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def set(self, **fields: object) -> None:
        self.data.update(fields)


class Tracer:
    def __init__(
        self,
        clock: Clock,
        run_id: str,
        path: Path | None = None,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.clock = clock
        self.run_id = run_id
        self.path = path
        self.events: list[TraceEvent] = []
        self.totals = Totals()
        self.listeners: list[Listener] = []
        self._timer = timer
        self._fh: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    def emit(self, kind: str, **data: object) -> TraceEvent:
        event = TraceEvent(
            seq=len(self.events),
            ts=self.clock.now().isoformat(),
            run_id=self.run_id,
            kind=kind,
            data={k: redact(to_json(v)) for k, v in data.items()},
        )
        self.events.append(event)
        self._account(event)
        if self._fh is not None:
            self._fh.write(event.model_dump_json() + "\n")
            self._fh.flush()
        for listener in list(self.listeners):
            listener(event)
        return event

    @contextmanager
    def span(self, kind: str, **data: object) -> Iterator[Span]:
        span = Span()
        span.set(**data)
        start = self._timer()
        try:
            yield span
        finally:
            span.set(latency_ms=round((self._timer() - start) * 1000, 3))
            self.emit(kind, **span.data)

    def of_kind(self, kind: str) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == kind]

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _account(self, event: TraceEvent) -> None:
        d = event.data
        if event.kind == "model.call":
            self.totals.model_calls += 1
            self.totals.input_tokens += _as_int(d.get("input_tokens"))
            self.totals.output_tokens += _as_int(d.get("output_tokens"))
            cost = d.get("cost_usd")
            if isinstance(cost, int | float):
                self.totals.cost_usd += float(cost)
        elif event.kind == "tool.call":
            self.totals.tool_calls += 1
            latency = d.get("latency_ms")
            if isinstance(latency, int | float):
                self.totals.tool_latencies_ms.append(float(latency))


def to_json(value: object) -> JsonValue:
    """Normalise any value (models, datetimes, enums, tuples) to plain JSON."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    loaded: JsonValue = json.loads(json.dumps(value, default=str))
    return loaded


def _as_int(value: JsonValue) -> int:
    return value if isinstance(value, int) else 0


def load_trace(path: Path) -> list[TraceEvent]:
    with path.open(encoding="utf-8") as fh:
        return [TraceEvent.model_validate(json.loads(line)) for line in fh if line.strip()]
