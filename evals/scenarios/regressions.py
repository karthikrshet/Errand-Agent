"""Scenarios built from defects an independent review reproduced (docs/BUGS.md #5-#9).

Each one is the reviewer's reproduction, kept as a permanent eval.
"""

from __future__ import annotations

from errand.agent.state import State
from errand.domain.time import to_ist
from errand.llm.script import nlu
from errand.tools.scenarios import get_scenario
from evals.harness import Do, EvalRun, EvalScenario, ProactiveCheck, ProviderCancels, Say
from evals.scenarios.common import (
    SUNDAY,
    TODAY,
    YES,
    Checks,
    ask_plumber,
    checks,
    final_state,
    pick,
    pick_provider,
)


def _mark_forgot_koramangala(r: EvalRun) -> None:
    r.forgotten_values.append("Koramangala")
    r.forget_from_request = len(r.llm.requests)


@checks
def _forget_after_use(r: EvalRun, c: Checks) -> None:
    after_forget = r.replies[2].state
    c.that(after_forget.request.area is None, "forgotten area still in the request")
    c.that("area" not in after_forget.sources, "provenance of forgotten field kept")
    last = r.replies[-1]
    c.that("which area" in last.text, "did not ask for the area again after forgetting it")
    c.that(
        not any("Koramangala" in t.text for t in r.world.store.turns("u_demo")),
        "value left in stored history",
    )
    forget = r.tracer.of_kind("memory.forget")
    c.that(
        forget and forget[0].data.get("request_fields_cleared") == ["area"],
        "forget did not report clearing the request field",
    )
    c.that(final_state(r) == State.GATHERING, f"final {final_state(r)}")


FORGET_AFTER_USE = EvalScenario(
    "21b",
    "memory_forget_after_value_used",
    "A remembered area has already filled the live request; 'forget my area' clears it from the "
    "request, history and every later prompt, and the next search asks for the area again",
    [
        Say("I'm in Koramangala, remember that", nlu("request", area="Koramangala", remember=True)),
        Say(
            "Find a plumber this week",
            nlu("request", service="plumber", date_start=TODAY, date_end=SUNDAY),
        ),
        Say("forget my area", nlu("memory_forget", memory={"key": "preferred_area"})),
        Do(_mark_forgot_koramangala),
        Say("search again", nlu("request")),
    ],
    _forget_after_use,
)

LONG_MESSAGE = "plumber please " + ("my kitchen tap is leaking badly " * 600)


@checks
def _long_message(r: EvalRun, c: Checks) -> None:
    calls = [e for e in r.tracer.of_kind("model.call") if e.data.get("purpose") == "nlu"]
    c.that(calls, "no model call")
    m = calls[0].data["context_manifest"]
    assert isinstance(m, dict)
    c.that(m["user_message_truncated"] is True, "oversized message was not capped")
    c.that(r.tracer.of_kind("context.user_message_truncated"), "truncation not traced")
    c.that("which area" in r.last.text, "agent did not continue normally")


LONG_MESSAGE_BUDGET = EvalScenario(
    "23b",
    "context_budget_oversized_message",
    "A ~4k-token user message is capped to its share of the budget; the exact prompt stays "
    "under the hard limit (I4) and the turn proceeds normally",
    [Say(LONG_MESSAGE, nlu("request", service="plumber"))],
    _long_message,
)


@checks
def _stale_card(r: EvalRun, c: Checks) -> None:
    c.that(r.replies[2].state.pending_approval is None, "re-search kept the old approval card")
    c.that(not r.tracer.of_kind("approval.granted"), "a grant was minted for a card not shown")
    c.that(not r.world.store.all_intents(), "an intent was opened")
    c.that("nothing waiting for your confirmation" in r.last.text, "no guidance after 'yes'")
    c.that(not r.tracer.of_kind("run.error"), "generic error surfaced")


STALE_CARD = EvalScenario(
    "09b",
    "stale_approval_after_re_search",
    "Card shown -> user changes a non-material detail (job) -> re-search -> 'yes' must not "
    "confirm the card the user is no longer looking at",
    [
        ask_plumber(),
        pick(1),
        Say("it's for the bathroom tap actually", nlu("request", job="bathroom tap")),
        YES,
    ],
    _stale_card,
    get_scenario("happy_path"),
)


@checks
def _target_gone(r: EvalRun, c: Checks) -> None:
    c.that(not r.tool_calls("cancel"), "cancel was sent for a booking the provider already ended")
    c.that(not r.tracer.of_kind("approval.granted"), "grant minted for a dead target")
    c.that("no longer active" in r.last.text, "user not told why")
    c.that("Cancelled and verified" not in r.last.text, "false success claim")


TARGET_GONE = EvalScenario(
    "17b",
    "cancel_target_cancelled_by_provider_meanwhile",
    "Cancel card shown -> provider cancels that booking -> 'yes' re-checks the booking, sends "
    "no cancel and does not claim 'Cancelled and verified'",
    [
        Say("cancel my AC servicing", nlu("cancel_booking", booking_ref={"service": "ac_service"})),
        ProviderCancels("b_up_ac01"),
        YES,
    ],
    _target_gone,
)


@checks
def _ambiguous_change(r: EvalRun, c: Checks) -> None:
    second = r.replies[2].state.pending_approval
    c.that(second is not None and to_ist(second.slot).hour == 11, "requested 11am not acted on")
    c.that(len(r.tracer.of_kind("approval.granted")) == 1, "ambiguous reply granted")
    c.that("haven't done anything" in r.replies[2].text, "ambiguity not explained")
    rows = r.world.env.conn.execute(
        "SELECT slot FROM bookings WHERE idempotency_key IS NOT NULL"
    ).fetchall()
    c.that(len(rows) == 1 and rows[0]["slot"].endswith("T11:00:00+05:30"), "not booked at 11")


AMBIGUOUS_CHANGE = EvalScenario(
    "33b",
    "ambiguous_confirmation_with_change",
    "'yes but make it 11am' never grants; the change is acted on (new hold, new card) and only "
    "the next explicit 'yes' books the 11am slot",
    [
        ask_plumber(),
        pick_provider("pl_aqua"),
        Say(
            "yes but make it 11am",
            nlu("select_option", selection={"provider_id": "pl_aqua", "hour": 11}),
        ),
        YES,
    ],
    _ambiguous_change,
    get_scenario("happy_path"),
)


@checks
def _deferred(r: EvalRun, c: Checks) -> None:
    told = r.replies[1]
    c.that("cancelled your booking" in told.text, "user not told during the task")
    c.that(told.state.status == State.PRESENTING_OPTIONS, "watcher hijacked the user's task")
    c.that(r.replies[2].state.status == State.AWAITING_APPROVAL, "user's own task broken")
    c.that(r.replies[3].state.status == State.COMPLETED, "user's booking not completed")
    offer = r.replies[4]
    c.that(offer.state.status == State.AWAITING_APPROVAL, "replacement never offered after task")
    req = offer.state.pending_approval
    c.that(req is not None and req.provider_id != "ac_cool", "re-offered the cancelling provider")
    notices = [t for t in r.texts() if "cancelled your booking" in t]
    c.that(len(notices) == 2, f"notified {len(notices)} times (expected: told, then offered)")


DEFERRED_PROACTIVE = EvalScenario(
    "18b",
    "proactive_cancellation_while_user_busy",
    "Provider cancels while the user is mid-booking -> told once, task not hijacked -> once the "
    "user's booking completes, the next watcher tick offers a replacement for fresh approval",
    [
        ask_plumber(),
        ProviderCancels("b_up_ac01"),
        ProactiveCheck(),
        pick(1),
        YES,
        ProactiveCheck(),
    ],
    _deferred,
    get_scenario("happy_path"),
)


@checks
def _default_odds(r: EvalRun, c: Checks) -> None:
    """Outcome is random here; correctness must hold whichever one occurred."""
    st = final_state(r)
    booked = r.new_bookings()
    c.that(booked <= 1, f"{booked} bookings from one approval")
    if st == State.COMPLETED:
        c.that(booked == 1, "claimed completion with no booking in the ledger")
        c.that("verified" in r.last.text, "completed without a verified message")
    elif st == State.PRESENTING_OPTIONS:
        c.that(booked == 0 and "just taken" in r.last.text, "slot_taken handled wrongly")
    else:
        c.that(st == State.FAILED_SAFE and "NOT" in r.last.text, f"unexpected end state {st}")


DEFAULT_ODDS = EvalScenario(
    "34",
    "default_odds_end_to_end",
    "No forced outcomes: the mock draws book() from the seeded 70/15/15 distribution; whatever "
    "happens, at most one booking and the user-facing claim matches the ledger",
    [ask_plumber(), pick(1), YES],
    _default_odds,
)

SCENARIOS = [
    DEFAULT_ODDS,
    FORGET_AFTER_USE,
    LONG_MESSAGE_BUDGET,
    STALE_CARD,
    TARGET_GONE,
    AMBIGUOUS_CHANGE,
    DEFERRED_PROACTIVE,
]
