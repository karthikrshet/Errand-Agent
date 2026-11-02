"""Scenario runner with a simulated user and a scripted model.

A scenario is a list of steps. `Say` is one simulated-user turn and carries the raw output the
(scripted) model will return if NLU is consulted for that turn; deterministic approval handling
means the model is often *not* consulted, which is part of what the evals prove. Other steps
manipulate the world: advance the fake clock, arm a crash point, restart the process, fire a
provider cancellation, or send two confirmations concurrently.

After every scenario the global invariant checker (evals/invariants.py) runs independently of
the scenario's own checks.
"""

from __future__ import annotations

import random
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errand.agent.crash import CrashHooks, CrashPoint, SimulatedCrash
from errand.agent.orchestrator import Agent, Reply
from errand.agent.store import AgentStore
from errand.app import World, build_world
from errand.clock import FakeClock
from errand.config import Settings
from errand.llm.base import LLMError, LLMRequest
from errand.llm.script import nlu
from errand.llm.scripted import ScriptedClient
from errand.memory.store import MemoryStore
from errand.observability.trace import Tracer
from errand.tools.scenarios import Scenario as WorldScenario

SESSION = "eval"
DEFAULT_REVIEW = (
    '{"sentiment": "positive", "themes": ["quality_good", "punctual"], "red_flags": [], '
    '"injection_suspected": false}'
)
DEFAULT_SUMMARY = (
    '{"facts": ["user has past electrician and plumber bookings", '
    '"user works from home on Tuesdays"], "open_items": ["kitchen tap drips"]}'
)
Text = str | Callable[["EvalRun"], str]
ModelOutput = str | Exception | Callable[[LLMRequest], str] | None


@dataclass
class Say:
    text: Text
    model: ModelOutput = None
    session: str = SESSION


@dataclass
class Advance:
    seconds: float


@dataclass
class ArmCrash:
    point: CrashPoint


@dataclass
class Restart:
    """Simulate a process restart: new Agent and stores on the same DB files, then startup."""

    session: str = SESSION


@dataclass
class ProviderCancels:
    booking_id: str


@dataclass
class ProactiveCheck:
    session: str = SESSION


@dataclass
class Concurrent:
    """Send the same text from N separate 'processes' (Agents) at the same instant."""

    text: Text
    n: int = 2
    model: ModelOutput = None


@dataclass
class Do:
    """Escape hatch for world manipulation (e.g. seeding memories)."""

    fn: Callable[[EvalRun], None]


Step = Say | Advance | ArmCrash | Restart | ProviderCancels | ProactiveCheck | Concurrent | Do


@dataclass
class EvalScenario:
    sid: str
    name: str
    expected: str
    steps: Sequence[Step]
    check: Callable[[EvalRun], list[str]]
    world: WorldScenario = field(default_factory=WorldScenario)
    settings: dict[str, Any] = field(default_factory=dict)
    seed: int = 7


@dataclass
class EvalRun:
    scenario: EvalScenario
    workdir: Path
    clock: FakeClock
    settings: Settings
    tracer: Tracer
    crash: CrashHooks
    world: World
    llm: ScriptedClient
    replies: list[Reply] = field(default_factory=list)
    crashes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    current_model: ModelOutput = None
    # Raw values the simulated user asked to forget; I4 greps every prompt for them.
    forgotten_values: list[str] = field(default_factory=list)
    forget_from_request: int = 0
    # What the (tool-less) summariser returns; scenarios may make it gullible.
    summary_output: str = DEFAULT_SUMMARY
    review_output: str = DEFAULT_REVIEW

    # --- convenience accessors used by checks ---

    @property
    def last(self) -> Reply:
        return self.replies[-1]

    def texts(self) -> list[str]:
        return [r.text for r in self.replies]

    def code(self) -> str:
        """The confirm code currently shown to the user."""
        st = self.world.agent.load(SESSION)
        assert st.pending_approval is not None, "no pending approval to confirm"
        return st.pending_approval.confirm_code

    def tool_calls(self, name: str | None = None) -> list[dict[str, Any]]:
        calls = [e.data for e in self.tracer.of_kind("tool.call")]
        return [c for c in calls if name is None or c.get("tool") == name]

    def new_bookings(self) -> int:
        return self.world.env.count_bookings() - 6

    def ledger_row(self, booking_id: str) -> dict[str, Any] | None:
        row = self.world.env.conn.execute(
            "SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)
        ).fetchone()
        return None if row is None else dict(row)


def _handler(run_ref: list[EvalRun]) -> Callable[[LLMRequest], str]:
    def handler(request: LLMRequest) -> str:
        if request.purpose == "history_summary":
            return run_ref[0].summary_output
        if request.purpose == "review_summary":
            return run_ref[0].review_output
        output = run_ref[0].current_model
        if callable(output) and not isinstance(output, Exception):
            return output(request)
        if isinstance(output, Exception):
            raise output
        if output is None:
            return nlu("other")
        return output

    return handler


def _build(run: EvalRun | None, sc: EvalScenario, workdir: Path, **kw: Any) -> World:
    return build_world(
        settings=kw["settings"],
        llm=kw["llm"],
        scenario=sc.world,
        clock=kw["clock"],
        db_path=workdir / "agent.db",
        ledger_path=workdir / "ledger.db",
        crash=kw["crash"],
        tracer=kw["tracer"],
    )


def start(sc: EvalScenario, workdir: Path) -> EvalRun:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    settings = Settings(seed=sc.seed, **sc.settings)
    clock = FakeClock()
    tracer = Tracer(clock, f"eval-{sc.sid}", workdir / "trace.jsonl")
    crash = CrashHooks()
    run_ref: list[EvalRun] = []
    llm = ScriptedClient(_handler(run_ref), model="scripted")
    world = _build(None, sc, workdir, settings=settings, llm=llm, clock=clock, crash=crash,
                   tracer=tracer)  # fmt: skip
    run = EvalRun(sc, workdir, clock, settings, tracer, crash, world, llm)
    run_ref.append(run)
    return run


def _text(run: EvalRun, text: Text) -> str:
    return text(run) if callable(text) else text


def execute(sc: EvalScenario, workdir: Path) -> EvalRun:
    run = start(sc, workdir)
    for step in sc.steps:
        try:
            _step(run, step)
        except SimulatedCrash as exc:
            run.crashes.append(exc.point)
        except Exception as exc:  # a harness-level failure is a scenario failure, not a crash
            run.errors.append(f"{type(exc).__name__}: {exc}")
            break
    run.tracer.close()
    return run


def _step(run: EvalRun, step: Step) -> None:
    if isinstance(step, Say):
        run.current_model = step.model
        run.replies.append(run.world.agent.handle_message(step.session, _text(run, step.text)))
    elif isinstance(step, Advance):
        run.clock.advance(step.seconds)
    elif isinstance(step, ArmCrash):
        run.crash.armed.add(step.point)
    elif isinstance(step, Restart):
        run.world = _build(run, run.scenario, run.workdir, settings=run.settings, llm=run.llm,
                           clock=run.clock, crash=run.crash, tracer=run.tracer)  # fmt: skip
        run.replies.append(run.world.agent.startup(step.session))
    elif isinstance(step, ProviderCancels):
        run.world.env.simulate_provider_cancellation(step.booking_id)
    elif isinstance(step, ProactiveCheck):
        run.replies.append(run.world.agent.proactive_check(step.session))
    elif isinstance(step, Concurrent):
        _concurrent(run, step)
    else:
        step.fn(run)


def _concurrent(run: EvalRun, step: Concurrent) -> None:
    """N independent Agents (as if N processes) share the DB files; only the DB serialises."""
    run.current_model = step.model
    text = _text(run, step.text)
    agents = [
        Agent(
            settings=run.settings,
            clock=run.clock,
            llm=run.llm,
            transport=run.world.env.__class__(
                run.workdir / "ledger.db",
                user_id=run.settings.user_id,
                clock=run.clock,
                rng=random.Random(i),
                scenario=run.world.env.scenario,
                settings=run.settings,
            ),
            store=AgentStore(run.workdir / "agent.db"),
            memory=MemoryStore(run.workdir / "agent.db"),
            tracer_for=lambda _s: run.tracer,
            crash=run.crash,
        )
        for i in range(step.n)
    ]
    barrier = threading.Barrier(step.n)
    results: list[Reply] = []
    lock = threading.Lock()

    def worker(agent: Agent) -> None:
        barrier.wait()
        reply = agent.handle_message(SESSION, text)
        with lock:
            results.append(reply)

    threads = [threading.Thread(target=worker, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    run.replies.extend(results)


def model_error(message: str = "provider unavailable") -> LLMError:
    return LLMError(message)
