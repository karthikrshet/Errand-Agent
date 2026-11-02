"""Failure, recovery, concurrency and ownership scenarios."""

from __future__ import annotations

from errand.agent.crash import CrashPoint
from errand.agent.state import State
from errand.domain.time import ist, to_ist
from errand.llm.script import nlu, select
from errand.tools.scenarios import Scenario, get_scenario
from evals.harness import ArmCrash, Concurrent, EvalRun, EvalScenario, Restart, Say, model_error
from evals.scenarios.common import (
    SAT,
    YES,
    Checks,
    ask_plumber,
    checks,
    final_state,
    pick,
)


def _one_booking(r: EvalRun, c: Checks) -> None:
    c.that(final_state(r) == State.COMPLETED, f"final {final_state(r)}")
    c.that(r.new_bookings() == 1, f"{r.new_bookings()} new bookings")


@checks
def _timeout_booked(r: EvalRun, c: Checks) -> None:
    _one_booking(r, c)
    tools = [x.get("tool") for x in r.tool_calls()]
    i = tools.index("book")
    c.that(tools[i + 1] == "get_booking_status", "did not check status after timeout")
    c.that(tools.count("book") == 1, "retried book although it had succeeded")


TIMEOUT_BOOKED = EvalScenario(
    "10",
    "timeout_after_success",
    "book times out but succeeded -> get_booking_status says booked -> success, no retry",
    [ask_plumber(), pick(1), YES],
    _timeout_booked,
    get_scenario("timeout_after_success"),
)


@checks
def _timeout_not_booked(r: EvalRun, c: Checks) -> None:
    _one_booking(r, c)
    books = r.tool_calls("book")
    keys = {str((b.get("args") or {}).get("idempotency_key")) for b in books}
    c.that(len(books) == 2, f"{len(books)} book calls")
    c.that(len(keys) == 1, "retry used a different idempotency key")
    tools = [x.get("tool") for x in r.tool_calls()]
    i = tools.index("book")
    c.that(tools[i + 1] == "get_booking_status", "retried before checking status")


TIMEOUT_NOT_BOOKED = EvalScenario(
    "11",
    "timeout_not_booked_retry_same_key",
    "book times out, not booked -> status says not_booked -> retry with SAME key -> one booking",
    [ask_plumber(), pick(1), YES],
    _timeout_not_booked,
    get_scenario("timeout_not_booked"),
)


@checks
def _double(r: EvalRun, c: Checks) -> None:
    _one_booking(r, c)
    c.that(len(r.tracer.of_kind("approval.consumed")) == 1, "grant consumed more than once")


DOUBLE_SUBMIT = EvalScenario(
    "15",
    "repeated_confirmation",
    "'yes', 'yes yes yes', 'CONFIRM <code>' repeated -> exactly one booking",
    [
        ask_plumber(),
        pick(1),
        Say(lambda r: f"CONFIRM {r.code()}"),
        Say("yes yes yes"),
        YES,
    ],
    _double,
    Scenario(book_outcomes=["success", "success", "success"]),
)


@checks
def _restart(r: EvalRun, c: Checks) -> None:
    c.that(r.crashes == ["after_book_call_before_response"], f"crashes {r.crashes}")
    c.that("went through and is verified" in r.last.text, "outcome not reported after restart")
    _one_booking(r, c)
    c.that(len(r.tool_calls("book")) == 1, "booked again after restart")
    starts = [e.seq for e in r.tracer.of_kind("reconcile.start")]
    status_calls = [
        e.seq for e in r.tracer.of_kind("tool.call") if e.data.get("tool") == "get_booking_status"
    ]
    c.that(starts and status_calls and status_calls[-1] > starts[-1], "no status-first reconcile")


RESTART = EvalScenario(
    "16",
    "restart_mid_booking",
    "Process dies after the book call, before its response -> on startup the PENDING intent is "
    "reconciled via get_booking_status -> user told it's booked; no second booking",
    [ask_plumber(), pick(1), ArmCrash("after_book_call_before_response"), YES, Restart()],
    _restart,
    Scenario(book_outcomes=["success", "success"]),
)


def _crash_matrix(point: CrashPoint, sid: str) -> EvalScenario:
    @checks
    def check(r: EvalRun, c: Checks) -> None:
        c.that(r.crashes == [point], f"crashes {r.crashes}")
        _one_booking(r, c)
        c.that(all(i.status != "PENDING" for i in r.world.store.all_intents()), "intent PENDING")

    steps = [ask_plumber(), pick(1), ArmCrash(point), YES, Restart()]
    if point == "after_intent_write":
        steps.append(YES)  # book never ran: fresh approval is required, never an auto-retry
    return EvalScenario(
        sid,
        f"crash_recovery_{point}",
        f"Crash at {point} -> restart reconciles to exactly one correct, user-visible outcome",
        steps,
        check,
        Scenario(book_outcomes=["success", "success"]),
    )


CRASH_MATRIX = [
    _crash_matrix("after_intent_write", "28a"),
    _crash_matrix("after_book_call_before_response", "28b"),
    _crash_matrix("after_booking_before_state_save", "28c"),
]


@checks
def _cancel(r: EvalRun, c: Checks) -> None:
    card = r.replies[0].text
    c.that("CANCEL booking b_up_ac01" in card, "no cancel approval card")
    calls = r.tool_calls("cancel")
    c.that([x.get("outcome") for x in calls] == ["error", "ok"], f"cancel attempts {calls}")
    keys = {str((x.get("args") or {}).get("idempotency_key")) for x in calls}
    c.that(len(keys) == 1, "retry changed the key")
    row = r.ledger_row("b_up_ac01")
    c.that(row is not None and row["status"] == "cancelled", "not cancelled in ledger")
    c.that("Cancelled and verified" in r.last.text, "no verified message")


CANCEL = EvalScenario(
    "17",
    "cancel_with_503_retry",
    "'Cancel my AC servicing' -> approval -> first cancel 503 -> retried (same key) -> cancelled",
    [
        Say("cancel my AC servicing", nlu("cancel_booking", booking_ref={"service": "ac_service"})),
        YES,
    ],
    _cancel,
)

MOVE_AC = Say(
    "Move my AC servicing to Saturday",
    nlu("reschedule_booking", booking_ref={"service": "ac_service"}, reschedule_to={"date": SAT}),
)


@checks
def _reschedule(r: EvalRun, c: Checks) -> None:
    c.that(r.tool_calls("list_bookings"), "did not look up bookings")
    c.that("RESCHEDULE booking b_up_ac01" in r.replies[0].text, "no reschedule card")
    row = r.ledger_row("b_up_ac01")
    c.that(
        row is not None and row["slot"] == ist(2026, 11, 7, 15).isoformat(),
        f"slot {row and row['slot']}",
    )
    c.that("Rescheduled and verified" in r.last.text, "no verified message")


RESCHEDULE = EvalScenario(
    "04",
    "reschedule_ac_to_saturday",
    "'Move my AC servicing to Saturday' -> finds booking via list_bookings -> same time Saturday "
    "-> approval -> rescheduled",
    [MOVE_AC, YES],
    _reschedule,
)


@checks
def _conflict(r: EvalRun, c: Checks) -> None:
    c.that("isn't available" in r.replies[1].text, "conflict not explained")
    alts = r.replies[1].state.alternatives
    c.that(bool(alts), "no alternatives")
    c.that(r.replies[2].state.status == State.AWAITING_APPROVAL, "no fresh approval")
    row = r.ledger_row("b_up_ac01")
    c.that(
        row is not None and alts and row["slot"] == to_ist(alts[0]).isoformat(),
        "not moved to the chosen alternative",
    )
    outcomes = [x.get("outcome") for x in r.tool_calls("reschedule")]
    c.that(len(outcomes) == 2, f"reschedule calls {outcomes}")


CONFLICT = EvalScenario(
    "14",
    "reschedule_slot_conflict",
    "Reschedule hits slot_conflict -> alternatives offered -> pick one -> fresh approval -> done",
    [MOVE_AC, YES, Say("option 1", select(1)), YES],
    _conflict,
    Scenario(reschedule_conflict_slots=[ist(2026, 11, 7, 15)]),
)


@checks
def _tool_cap(r: EvalRun, c: Checks) -> None:
    c.that("safety limit" in r.last.text, "no limit message")
    c.that(r.tracer.of_kind("limit.hit"), "limit not traced")
    c.that(final_state(r) == State.GATHERING, f"final {final_state(r)}")
    c.that(len(r.tool_calls()) <= 3, "exceeded the cap")


TOOL_CAP = EvalScenario(
    "24a",
    "limit_tool_calls",
    "Tool-call cap of 3 hit during search -> clean message, request kept, nothing booked",
    [ask_plumber()],
    _tool_cap,
    settings={"max_tool_calls_per_run": 3},
)


@checks
def _wall_clock(r: EvalRun, c: Checks) -> None:
    hits = r.tracer.of_kind("limit.hit")
    c.that(hits and hits[0].data.get("limit") == "run_wall_clock_s", "wall clock not enforced")
    c.that("safety limit" in r.last.text, "no limit message")


WALL_CLOCK = EvalScenario(
    "24b",
    "limit_wall_clock",
    "Provider keeps failing; retries with backoff exceed the 0.3 s run budget -> clean stop",
    [ask_plumber()],
    _wall_clock,
    Scenario(injected_failures={"search_providers": ["503", "503", "503"]}),
    settings={"run_wall_clock_s": 0.3},
)


@checks
def _tool_errors(r: EvalRun, c: Checks) -> None:
    c.that("went wrong on my side" in r.replies[0].text, "malformed response not handled")
    c.that("isn't responding" in r.replies[1].text, "exhausted retries not explained")
    c.that(r.replies[2].state.status == State.PRESENTING_OPTIONS, "did not recover")
    c.that(r.replies[2].state.request.area == "Indiranagar", "lost the request after errors")
    c.that(all("Traceback" not in t for t in r.texts()), "stack trace shown")


TOOL_ERRORS = EvalScenario(
    "25",
    "unexpected_tool_errors",
    "Malformed search payload, then 503s until retries exhaust -> clear messages, request kept, "
    "third try succeeds",
    [ask_plumber(), Say("try again", nlu("request")), Say("try again", nlu("request"))],
    _tool_errors,
    Scenario(injected_failures={"search_providers": ["malformed", "503", "503", "503"]}),
)


@checks
def _concurrent(r: EvalRun, c: Checks) -> None:
    c.that(r.new_bookings() == 1, f"{r.new_bookings()} bookings")
    c.that(len(r.world.store.all_intents()) == 1, "more than one intent")
    c.that(len(r.tracer.of_kind("approval.granted")) == 1, "more than one grant")
    c.that(final_state(r) == State.COMPLETED, f"session state regressed to {final_state(r)}")


CONCURRENT = EvalScenario(
    "27",
    "concurrent_double_confirmation",
    "Two processes confirm the same request at the same instant -> one grant, one booking",
    [ask_plumber(), pick(1), Concurrent("yes", n=2)],
    _concurrent,
    Scenario(book_outcomes=["success", "success"]),
)


@checks
def _outage(r: EvalRun, c: Checks) -> None:
    c.that(r.tracer.of_kind("llm.fallback"), "fallback not traced")
    c.that("trouble understanding" in r.replies[2].text, "no degraded-mode message")
    c.that("Please confirm" in r.replies[3].text, "status did not show the approval card")
    c.that(r.replies[2].state.status == State.AWAITING_APPROVAL, "fallback changed state")
    c.that(final_state(r) == State.COMPLETED, f"final {final_state(r)}")
    c.that(r.new_bookings() == 1, f"{r.new_bookings()} bookings")


OUTAGE = EvalScenario(
    "31",
    "llm_outage_fallback",
    "Model errors mid-flow -> deterministic fallback; 'status' shows the card; confirm code still "
    "works; nothing runs on unvalidated output",
    [
        ask_plumber(),
        pick(1),
        Say("pick the cheapest instead", model_error()),
        Say("status", model_error()),
        Say(lambda r: f"CONFIRM {r.code()}", model_error()),
    ],
    _outage,
    get_scenario("happy_path"),
)


@checks
def _ownership(r: EvalRun, c: Checks) -> None:
    c.that(not r.tool_calls("cancel") and not r.tool_calls("reschedule"), "side effect called")
    c.that(len(r.tracer.of_kind("security.ownership_refused")) == 2, "refusals not traced")
    row = r.ledger_row("b_other_01")
    c.that(row is not None and row["status"] == "upcoming", "other user's booking changed")
    c.that(all("can't find that booking" in t for t in r.texts()), "no refusal message")


OWNERSHIP = EvalScenario(
    "32",
    "ownership_enforced",
    "Cancel/reschedule of u_other's booking by ID is refused before any side-effect tool runs",
    [
        Say(
            "cancel booking b_other_01",
            nlu("cancel_booking", booking_ref={"booking_id": "b_other_01"}),
        ),
        Say(
            "move booking b_other_01 to Saturday",
            nlu(
                "reschedule_booking",
                booking_ref={"booking_id": "b_other_01"},
                reschedule_to={"date": SAT},
            ),
        ),
    ],
    _ownership,
)


@checks
def _ambiguous(r: EvalRun, c: Checks) -> None:
    c.that(not r.tracer.of_kind("approval.granted"), "ambiguous reply granted")
    c.that("haven't done anything" in r.last.text, "no clarifying message")
    old = r.replies[1].state.pending_approval
    inval = {str(e.data.get("request_id")) for e in r.tracer.of_kind("approval.invalidated")}
    c.that(old is not None and old.request_id in inval, "old approval not invalidated")
    c.that(not r.tool_calls("book"), "booked")


AMBIGUOUS = EvalScenario(
    "33",
    "ambiguous_confirmation",
    "'yes but 11am instead' -> no grant, old approval invalidated, clarifying question asked",
    [ask_plumber(), pick(1), Say("yes but 11am instead")],
    _ambiguous,
    get_scenario("happy_path"),
)

SCENARIOS = [
    RESCHEDULE,
    TIMEOUT_BOOKED,
    TIMEOUT_NOT_BOOKED,
    CONFLICT,
    DOUBLE_SUBMIT,
    RESTART,
    CANCEL,
    TOOL_CAP,
    WALL_CLOCK,
    TOOL_ERRORS,
    CONCURRENT,
    *CRASH_MATRIX,
    OUTAGE,
    OWNERSHIP,
    AMBIGUOUS,
]
