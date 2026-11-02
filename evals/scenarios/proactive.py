"""Proactive provider-cancellation scenario."""

from __future__ import annotations

from errand.agent.state import State
from errand.tools.scenarios import get_scenario
from evals.harness import EvalRun, EvalScenario, ProactiveCheck, ProviderCancels
from evals.scenarios.common import YES, Checks, checks, final_state


@checks
def _proactive(r: EvalRun, c: Checks) -> None:
    starts = r.tracer.of_kind("run.start")
    proactive_run = starts[0]
    c.that(proactive_run.data.get("user_text") is None, "detection needed a user message")
    decisions = {str(e.data["booking_id"]): e.data for e in r.tracer.of_kind("proactive.decision")}
    c.that(decisions.get("b_hist_el01", {}).get("notify") is False, "past booking not skipped")
    c.that(decisions.get("b_up_ac01", {}).get("notify") is True, "upcoming cancellation ignored")
    first = r.replies[0]
    c.that("cancelled your booking" in first.text, "user not notified")
    c.that(first.state.status == State.AWAITING_APPROVAL, "no fresh approval requested")
    req = first.state.pending_approval
    c.that(req is not None and req.provider_id != "ac_cool", "re-offered the cancelling provider")
    c.that(not r.tool_calls("book") or r.tracer.of_kind("approval.granted"), "booked unasked")
    grants = r.tracer.of_kind("approval.granted")
    c.that(len(grants) == 1, f"{len(grants)} grants")
    c.that(final_state(r) == State.COMPLETED, f"final {final_state(r)}")
    row = r.ledger_row("b_up_ac01")
    c.that(row is not None and row["status"] == "cancelled_by_provider", "original changed")
    c.that(r.new_bookings() == 1, f"{r.new_bookings()} new bookings")
    tools = [x.get("tool") for x in r.tool_calls()]
    c.that(tools[0] == "list_bookings", "watcher did not poll the source of truth")
    again = r.replies[-1]
    c.that("cancelled" not in again.text, "re-notified an already handled cancellation")


PROACTIVE = EvalScenario(
    "18",
    "proactive_provider_cancellation",
    "Provider cancels upcoming AC service (and a past electrician visit) with no user message -> "
    "watcher notifies for the upcoming one only, holds a replacement from another provider "
    "within preferences, books only after fresh approval",
    [
        ProviderCancels("b_hist_el01"),
        ProviderCancels("b_up_ac01"),
        ProactiveCheck(),
        YES,
        ProactiveCheck(),
    ],
    _proactive,
    get_scenario("happy_path"),
)

SCENARIOS = [PROACTIVE]
