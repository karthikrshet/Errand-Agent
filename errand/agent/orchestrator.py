"""The run loop for one user turn.

Order matters and is the core safety argument:
1. Recover: resolve PENDING intents against the source of truth before reading new input.
2. If an approval is pending, the deterministic confirmation rules see the raw user text first.
   Only an AFFIRM from this channel can mint a grant; the LLM is not consulted.
3. Otherwise the LLM proposes an intent + slot patch; code validates and executes.
4. A top-level guard turns every failure into a user-facing message and a saved state.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from errand.agent import booking, manage_flow, search_flow
from errand.agent import responses as r
from errand.agent.approval import Decision, classify_confirmation
from errand.agent.approval_flow import grant_from_user_message, invalidate_pending, reissue
from errand.agent.crash import CrashHooks
from errand.agent.limits import RunBudget
from errand.agent.nlu import NLU, SYSTEM_PROMPT, BookingRef, NLUResult
from errand.agent.policy import PolicyViolation
from errand.agent.runtime import RunContext
from errand.agent.state import TERMINAL, State, WorkflowState
from errand.agent.store import AgentStore, StaleState
from errand.clock import Clock
from errand.config import Settings
from errand.domain.request import age_sources
from errand.domain.time import to_ist
from errand.errors import ApprovalInvalid, IllegalTransition, LimitExceeded
from errand.ids import IdFactory
from errand.llm.base import LLMClient
from errand.memory import commands as memory_cmd
from errand.memory.context import ContextBuilder
from errand.memory.store import MemoryStore
from errand.observability.trace import Tracer
from errand.tools.gateway import RetriesExhausted, ToolGateway, Transport

TracerFactory = Callable[[str], Tracer]
MID_FLIGHT = frozenset({State.SEARCHING, State.HOLDING, State.EXECUTING, State.VERIFYING})


@dataclass
class Reply:
    text: str
    state: WorkflowState
    messages: list[str] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        *,
        settings: Settings,
        clock: Clock,
        llm: LLMClient,
        transport: Transport,
        store: AgentStore,
        tracer_for: TracerFactory,
        memory: MemoryStore,
        rng: random.Random | None = None,
        crash: CrashHooks | None = None,
    ) -> None:
        self.memory = memory
        self.settings = settings
        self.clock = clock
        self.llm = llm
        self.transport = transport
        self.store = store
        self.tracer_for = tracer_for
        self.rng = rng or random.Random(settings.seed)
        self.crash = crash or CrashHooks()
        self.ids = IdFactory(settings.seed, store.next_boot())
        self.context = ContextBuilder(
            settings.context_budget_tokens, settings.effective_context_budget
        )
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ public API

    def handle_message(self, session_id: str, text: str) -> Reply:
        with self._lock(session_id):
            return self._run(session_id, text)

    def startup(self, session_id: str) -> Reply:
        """Process (re)start: reconcile before accepting any new user input."""
        with self._lock(session_id):
            return self._run(session_id, None)

    def load(self, session_id: str) -> WorkflowState:
        state = self.store.load_state(session_id)
        return state or WorkflowState(session_id=session_id, user_id=self.settings.user_id)

    # ------------------------------------------------------------------ run

    def _run(self, session_id: str, text: str | None) -> Reply:
        state = self.load(session_id)
        tracer = self.tracer_for(session_id)
        budget = RunBudget(self.settings, self.clock)
        gateway = ToolGateway(
            self.transport,
            clock=self.clock,
            settings=self.settings,
            tracer=tracer,
            rng=self.rng,
            budget=budget,
        )
        ctx = RunContext(
            state=state,
            store=self.store,
            gateway=gateway,
            tracer=tracer,
            clock=self.clock,
            settings=self.settings,
            budget=budget,
            crash=self.crash,
            ids=self.ids,
            memory=self.memory,
        )
        if text is not None:
            ctx.user_turn_id = self.store.add_turn(
                session_id, state.user_id, "user", text, self.clock.now()
            )
        tracer.begin_turn(self.ids.new("turn"))
        tracer.emit(
            "run.start",
            session_id=session_id,
            user_id=state.user_id,
            turn_id=ctx.user_turn_id,
            user_text=text,
            state=state.status.value,
        )
        self._guarded(ctx, text)
        if not ctx.replies:
            ctx.say(r.didnt_understand() if text is not None else r.status([]))
        try:
            ctx.save()
        except StaleState:
            self._handled_elsewhere(ctx)
        state = ctx.state
        reply_text = "\n\n".join(ctx.replies)
        self.store.add_turn(session_id, state.user_id, "assistant", reply_text, self.clock.now())
        tracer.emit(
            "run.end",
            outcome=state.status.value,
            reply=reply_text,
            tool_calls=budget.tool_calls,
            model_calls=budget.model_calls,
            totals=tracer.totals.model_dump(exclude={"tool_latencies_ms"}),
        )
        tracer.end_turn()
        return Reply(text=reply_text, state=state, messages=list(ctx.replies))

    def _guarded(self, ctx: RunContext, text: str | None) -> None:
        """No exception escapes to the user. SimulatedCrash (BaseException) is not caught."""
        try:
            booking.recover(ctx)
            if text is not None:
                self._route(ctx, text)
        except StaleState:
            self._handled_elsewhere(ctx)
        except LimitExceeded as exc:
            ctx.tracer.emit("limit.hit", limit=exc.limit, value=exc.value)
            ctx.say(r.limit_hit(exc.limit))
            self._stop_mid_flight(ctx, f"limit {exc.limit}")
        except RetriesExhausted as exc:
            ctx.say(r.tool_unavailable(exc.tool))
            self._stop_mid_flight(ctx, f"{exc.tool} exhausted")
        except PolicyViolation as exc:
            ctx.tracer.emit("policy.violation", error=str(exc))
            ctx.say(r.policy_refused())
            self._stop_mid_flight(ctx, "policy violation")
        except ApprovalInvalid as exc:
            invalidate_pending(ctx, str(exc))
            ctx.say(r.approval_invalidated(str(exc)))
            self._stop_mid_flight(ctx, "approval invalid")
        except (IllegalTransition, Exception) as exc:
            ctx.tracer.emit("run.error", error=type(exc).__name__, message=str(exc))
            ctx.say(r.unexpected_error())
            self._stop_mid_flight(ctx, type(exc).__name__)

    def _handled_elsewhere(self, ctx: RunContext) -> None:
        """Lost a race with another process on this session: adopt its state, never overwrite."""
        fresh = self.load(ctx.state.session_id)
        ctx.tracer.emit("session.stale_write_rejected", kept_status=fresh.status.value)
        ctx.state = fresh
        ctx.replies[:] = [r.handled_elsewhere(fresh.status.value.replace("_", " ").lower())]

    def _stop_mid_flight(self, ctx: RunContext, reason: str) -> None:
        """Never leave a run 'in the middle' of a step.

        A failed read-only step falls back to the last resting state, keeping what the user
        told us; a failure during execution goes to FAILED_SAFE (any PENDING intent is left for
        reconciliation, so nothing is lost or double-booked).
        """
        st = ctx.state
        fallback = {State.SEARCHING: State.GATHERING, State.HOLDING: State.PRESENTING_OPTIONS}
        to = fallback.get(st.status)
        if to is None and st.status in MID_FLIGHT:
            to = State.FAILED_SAFE
        if to is not None and st.can_transition(to):
            ctx.move(to, "system", reason)

    # ------------------------------------------------------------------ routing

    def _route(self, ctx: RunContext, text: str) -> None:
        st = ctx.state
        st.counters.turns += 1
        st.sources = age_sources(st.sources)
        if st.status == State.COMPLETED and _is_affirmation(text):
            ctx.say(r.already_done(self._last_booking_id(ctx)))
            return
        if st.status in TERMINAL:
            ctx.move(State.IDLE, "user_message", "new workflow")
            st.reset_workflow()
        if st.pending_approval is not None and self._handle_confirmation(ctx, text):
            return
        result = self._interpret(ctx, text)
        if result is None:
            self._fallback(ctx, text)
            return
        ctx.tracer.emit("nlu.result", intent=result.intent, result=result)
        self._dispatch(ctx, result)

    def _handle_confirmation(self, ctx: RunContext, text: str) -> bool:
        """Returns True when the message was fully handled as an approval reply."""
        decision, grant = grant_from_user_message(ctx, text)
        req = ctx.state.pending_approval
        assert req is not None
        if grant is not None:
            booking.execute(ctx, grant)
            return True
        if decision == Decision.AFFIRM:
            ctx.say(r.already_confirmed())
            return True
        if decision == Decision.AMBIGUOUS:
            ctx.say(r.ambiguous_confirmation())
            reissue(ctx, "ambiguous reply")
            return True
        if decision == Decision.CODE_MISMATCH:
            ctx.say(r.code_mismatch(req))
            return True
        if decision == Decision.REJECT:
            invalidate_pending(ctx, "user declined")
            ctx.say(r.rejected())
            self._after_reject(ctx)
            return True
        return False

    def _after_reject(self, ctx: RunContext) -> None:
        st = ctx.state
        if st.kind == "booking" and st.candidates:
            ctx.move(State.PRESENTING_OPTIONS, "user_message", "declined approval")
        else:
            ctx.move(State.ABANDONED, "user_message", "declined approval")

    def _interpret(self, ctx: RunContext, text: str) -> NLUResult | None:
        memories, excluded = self.memory.select_for_context(ctx.user_id, ctx.state.request)
        built = self.context.build(
            system_prompt=SYSTEM_PROMPT,
            user_text=text,
            state=ctx.state,
            memories=memories,
            excluded=excluded,
            summary=ctx.state.history_summary,
            turns=self.store.turns(ctx.user_id, limit=self.settings.recent_turns)[:-1],
        )
        nlu = NLU(self.llm, self.settings, ctx.tracer, ctx.budget)
        ctx.state.counters.model_calls += 1
        today = to_ist(self.clock.now()).date()
        return nlu.interpret(text, built.text, today, built.manifest.model_dump(mode="json"))

    def _dispatch(self, ctx: RunContext, result: NLUResult) -> None:
        handlers: dict[str, Callable[[RunContext, NLUResult], None]] = {
            "request": self._on_request,
            "select_option": self._on_select,
            "cancel_booking": lambda c, res: manage_flow.start_cancel(c, res.booking_ref),
            "reschedule_booking": lambda c, res: manage_flow.start_reschedule(
                c, res.booking_ref, res.reschedule_to
            ),
            "status": lambda c, _: c.say(self._status(c)),
            "memory_show": lambda c, _: memory_cmd.show(c),
            "memory_update": self._on_memory_update,
            "memory_forget": self._on_memory_forget,
            "abort": self._on_abort,
        }
        handlers.get(result.intent, self._on_other)(ctx, result)

    def _on_request(self, ctx: RunContext, result: NLUResult) -> None:
        if ctx.state.status == State.RESCHEDULE_FLOW and result.patch.date_start is not None:
            manage_flow.propose_new_slot(ctx, result.patch.date_start, result.patch.earliest_hour)
        else:
            if result.remember:
                memory_cmd.remember_from_patch(ctx, result.patch, result.patch.service
                                               or ctx.state.request.service)  # fmt: skip
            search_flow.handle_request(ctx, result)

    def _on_memory_update(self, ctx: RunContext, result: NLUResult) -> None:
        if result.memory is None:
            ctx.say(r.didnt_understand())
            return
        memory_cmd.update(ctx, result.memory)

    def _on_memory_forget(self, ctx: RunContext, result: NLUResult) -> None:
        if result.memory is None:
            ctx.say(r.didnt_understand())
            return
        memory_cmd.forget(ctx, result.memory)

    def _on_select(self, ctx: RunContext, result: NLUResult) -> None:
        status = ctx.state.status
        if status == State.RESCHEDULE_FLOW:
            manage_flow.handle_alternative(ctx, result.selection)
        elif status in (State.PRESENTING_OPTIONS, State.AWAITING_APPROVAL):
            search_flow.handle_selection(ctx, result.selection)
        else:
            ctx.say(r.didnt_understand())

    def _on_abort(self, ctx: RunContext, _: NLUResult) -> None:
        invalidate_pending(ctx, "user aborted")
        if ctx.state.can_transition(State.ABANDONED):
            ctx.move(State.ABANDONED, "user_message", "user aborted")
        ctx.say(r.abandoned())

    def _on_other(self, ctx: RunContext, _: NLUResult) -> None:
        ctx.say(r.didnt_understand())
        if ctx.state.pending_approval is not None:
            ctx.say(r.approval_card(ctx.state.pending_approval))

    # ------------------------------------------------------------------ degraded mode

    def _fallback(self, ctx: RunContext, text: str) -> None:
        """LLM unavailable: deterministic commands only. Nothing runs on unvalidated output."""
        words = text.strip().split()
        command = words[0].lower() if words else ""
        if command == "status":
            ctx.say(self._status(ctx))
        elif command == "cancel" and len(words) == 2:
            manage_flow.start_cancel(ctx, BookingRef(booking_id=words[1]))
        else:
            ctx.say(r.llm_fallback(self._status(ctx)))

    def _status(self, ctx: RunContext) -> str:
        st = ctx.state
        lines = [f"Current step: {st.status.value.replace('_', ' ').lower()}."]
        if st.pending_approval is not None:
            lines.append(r.approval_card(st.pending_approval))
        if st.intent_id is not None:
            intent = ctx.store.intent(st.intent_id)
            if intent is not None and intent.result and intent.status == "SUCCEEDED":
                lines.append(f"Last verified booking: {intent.result.get('booking_id')}.")
        return r.status(lines)

    def _last_booking_id(self, ctx: RunContext) -> str | None:
        intent = ctx.store.intent(ctx.state.intent_id) if ctx.state.intent_id else None
        return intent.result.get("booking_id") if intent and intent.result else None

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())


def _is_affirmation(text: str) -> bool:
    return classify_confirmation(text, "") == Decision.AFFIRM
