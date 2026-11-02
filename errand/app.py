"""Composition root: wires settings, clock, mock world, store, LLM and tracer into an Agent.

CLI, web UI, tests and evals all build the system here, so they exercise identical wiring.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from errand.agent.crash import CrashHooks
from errand.agent.orchestrator import Agent
from errand.agent.store import AgentStore
from errand.clock import Clock, SystemClock
from errand.config import Settings
from errand.llm.base import LLMClient
from errand.observability.trace import Tracer
from errand.tools.mock_env import MockToolEnv
from errand.tools.scenarios import Scenario


@dataclass
class World:
    agent: Agent
    env: MockToolEnv
    store: AgentStore
    tracer: Tracer
    clock: Clock
    settings: Settings


def build_world(
    *,
    settings: Settings,
    llm: LLMClient,
    scenario: Scenario | None = None,
    clock: Clock | None = None,
    db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    trace_path: Path | None = None,
    crash: CrashHooks | None = None,
    tracer: Tracer | None = None,
    run_id: str = "run",
) -> World:
    clock = clock or SystemClock()
    db = db_path if db_path is not None else settings.db_path
    ledger = ledger_path if ledger_path is not None else Path(str(db) + ".ledger")
    env = MockToolEnv(
        ledger,
        user_id=settings.user_id,
        clock=clock,
        rng=random.Random(f"world:{settings.seed}"),
        scenario=scenario,
        settings=settings,
    )
    store = AgentStore(db)
    tracer = tracer or Tracer(clock, run_id, trace_path)
    agent = Agent(
        settings=settings,
        clock=clock,
        llm=llm,
        transport=env,
        store=store,
        tracer_for=lambda _session: tracer,
        rng=random.Random(f"agent:{settings.seed}"),
        crash=crash,
    )
    return World(agent=agent, env=env, store=store, tracer=tracer, clock=clock, settings=settings)
