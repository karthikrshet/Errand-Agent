"""Scenarios built from defects an independent review reproduced (docs/BUGS.md #5-#9).

Each one is the reviewer's reproduction, kept as a permanent eval.
"""

from __future__ import annotations

from errand.agent.state import State
from errand.llm.script import nlu
from evals.harness import Do, EvalRun, EvalScenario, Say
from evals.scenarios.common import SUNDAY, TODAY, Checks, checks, final_state


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

SCENARIOS = [FORGET_AFTER_USE]
