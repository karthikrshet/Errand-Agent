"""FastAPI demo UI: chat on the left, live trace on the right (Server-Sent Events).

Run: `make ui` (Anthropic model; needs ANTHROPIC_API_KEY) or, keyless,
`ERRAND_SCRIPT=demo make ui` (scripted model for the demo phrases).
`ERRAND_SCENARIO=timeout_after_success` forces mock-world outcomes for live failure demos.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from errand.agent import responses as r
from errand.app import World, build_world
from errand.clock import SystemClock
from errand.config import Settings
from errand.domain.time import to_ist
from errand.llm.base import LLMClient
from errand.llm.scripted import ScriptedClient
from errand.observability.trace import TraceEvent
from errand.tools.scenarios import get_scenario
from errand.web.demo_script import demo_script

STATIC = Path(__file__).parent / "static"
KEEPALIVE_TICKS = 150  # ~15 s at 0.1 s polling


class MessageIn(BaseModel):
    text: str


class CancelIn(BaseModel):
    booking_id: str


class TraceHub:
    """Fans tracer events out to every connected SSE client (thread-safe)."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue[TraceEvent]] = []
        self._lock = threading.Lock()

    def publish(self, event: TraceEvent) -> None:
        with self._lock:
            for q in self._subs:
                q.put(event)

    def subscribe(self) -> queue.Queue[TraceEvent]:
        q: queue.Queue[TraceEvent] = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[TraceEvent]) -> None:
        with self._lock:
            self._subs.remove(q)


class BackgroundWatcher:
    """Polls for provider cancellations (no user message involved) and queues what the
    agent says for the chat UI to pick up."""

    def __init__(self, world: World, sessions: set[str]) -> None:
        self.world = world
        self.sessions = sessions
        self._inbox: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def start(self, interval_s: float) -> None:
        threading.Thread(target=self._loop, args=(interval_s,), daemon=True).start()

    def take(self, sid: str) -> list[str]:
        with self._lock:
            return self._inbox.pop(sid, [])

    def _loop(self, interval_s: float) -> None:
        idle_reply = r.status([])
        while True:
            time.sleep(interval_s)
            for sid in list(self.sessions):
                reply = self.world.agent.proactive_check(sid)
                news = [m for m in reply.messages if m != idle_reply]
                if news:
                    with self._lock:
                        self._inbox.setdefault(sid, []).extend(news)


def _startup_messages(world: World, sid: str) -> list[str]:
    """Only surface startup output that says something (e.g. a reconciled booking)."""
    idle = r.status([])
    return [m for m in world.agent.startup(sid).messages if m != idle]


def _default_llm(settings: Settings) -> LLMClient:
    script = os.environ.get("ERRAND_SCRIPT")
    if script == "demo":
        return ScriptedClient.from_map(demo_script(to_ist(SystemClock().now()).date()))
    if script:
        return ScriptedClient.from_map(json.loads(Path(script).read_text(encoding="utf-8")))
    from errand.llm.anthropic_client import AnthropicClient  # noqa: PLC0415

    return AnthropicClient(settings)


def _default_world() -> World:
    settings = Settings()
    return build_world(
        settings=settings,
        llm=_default_llm(settings),
        scenario=get_scenario(os.environ.get("ERRAND_SCENARIO", "default")),
        trace_path=settings.traces_dir / "web.jsonl",
        run_id="web",
    )


def create_app(world: World | None = None, watch_interval_s: float | None = None) -> FastAPI:
    """`watch_interval_s`: background proactive watcher period (0 disables). Defaults to
    ERRAND_WATCH_INTERVAL_S (15 s) for the real app, and to 0 when a world is injected."""
    if watch_interval_s is None:
        env = float(os.environ.get("ERRAND_WATCH_INTERVAL_S", "15"))
        watch_interval_s = env if world is None else 0.0
    world = world or _default_world()
    hub = TraceHub()
    world.tracer.listeners.append(hub.publish)
    started: set[str] = set()
    app = FastAPI(title="Errand Agent")
    watcher = BackgroundWatcher(world, started)
    if watch_interval_s > 0:
        watcher.start(watch_interval_s)

    started_lock = threading.Lock()

    def ensure_started(sid: str) -> list[str]:
        with started_lock:  # handlers run on FastAPI's threadpool
            if sid in started:
                return []
            started.add(sid)
        return _startup_messages(world, sid)

    def reply_json(sid: str, messages: list[str]) -> dict[str, object]:
        st = world.agent.load(sid)
        req = st.pending_approval
        return {
            "messages": messages,
            "status": st.status.value,
            "approval": None
            if req is None
            else {
                "action": req.action,
                "code": req.confirm_code,
                "price_paise": req.price_paise,
                "previous_price_paise": req.previous_price_paise,
            },
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/sessions/{sid}/message")
    def message(sid: str, body: MessageIn) -> dict[str, object]:
        boot = ensure_started(sid)
        reply = world.agent.handle_message(sid, body.text)
        return reply_json(sid, [*boot, *reply.messages])

    @app.post("/api/sessions/{sid}/watch")
    def watch(sid: str) -> dict[str, object]:
        boot = ensure_started(sid)
        reply = world.agent.proactive_check(sid)
        return reply_json(sid, [*boot, *reply.messages])

    @app.get("/api/sessions/{sid}/inbox")
    def poll_inbox(sid: str) -> dict[str, object]:
        messages = watcher.take(sid)
        return reply_json(sid, messages) if messages else {"messages": []}

    @app.post("/api/demo/provider-cancel")
    def provider_cancel(body: CancelIn) -> dict[str, str]:
        world.env.simulate_provider_cancellation(body.booking_id)
        return {"status": "cancelled_by_provider", "booking_id": body.booking_id}

    @app.get("/api/trace")
    async def trace() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            q = hub.subscribe()
            idle = 0
            try:
                while True:
                    try:
                        event = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.1)
                        idle += 1
                        if idle % KEEPALIVE_TICKS == 0:
                            yield ": keep-alive\n\n"
                        continue
                    yield f"data: {event.model_dump_json()}\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def __getattr__(name: str) -> FastAPI:
    """`uvicorn errand.web.app:app` builds the app lazily (importing must not hit the network)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
