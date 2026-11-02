"""The context budget is hard: measured on the exact prompt, whatever the inputs."""

from __future__ import annotations

import pytest

from errand.agent.nlu import system_prompt
from errand.agent.state import Candidate, WorkflowState
from errand.agent.store import Turn
from errand.config import Settings
from errand.domain.time import ist
from errand.llm.tokens import count_tokens
from errand.memory.context import ContextBuilder, MemoryView


def _state(n_candidates: int) -> WorkflowState:
    st = WorkflowState(session_id="s", user_id="u_demo")
    st.candidates = [
        Candidate(provider_id=f"p{i}", name="X" * 200, price_paise=100, rating=4.0,
                  slot=ist(2026, 11, 3, 9), score=1.0, reasons=["r" * 300])
        for i in range(n_candidates)
    ]  # fmt: skip
    return st


@pytest.mark.parametrize(
    ("user_len", "n_candidates", "n_turns", "n_memories"),
    [(10, 0, 0, 0), (40_000, 0, 0, 0), (100, 40, 0, 0), (100, 3, 400, 80), (40_000, 40, 400, 80)],
)
def test_exact_prompt_never_exceeds_budget(
    user_len: int, n_candidates: int, n_turns: int, n_memories: int
) -> None:
    s = Settings()
    builder = ContextBuilder(s.context_budget_tokens, s.effective_context_budget)
    system = system_prompt(ist(2026, 11, 2).date())
    built = builder.build(
        system_prompt=system,
        user_text="leaking tap " * (user_len // 12),
        state=_state(n_candidates),
        memories=[MemoryView(memory_id=f"m{i}", key="preferred_area", value="Indiranagar " * 20,
                             kind="explicit", status="active") for i in range(n_memories)],
        summary="fact " * 2000,
        turns=[Turn(turn_id=i, role="user", text="hello " * 50) for i in range(n_turns)],
    )  # fmt: skip
    exact = count_tokens(system) + count_tokens(built.text)
    assert exact == built.manifest.total_tokens
    assert exact <= s.effective_context_budget
    assert built.manifest.user_message_truncated == (user_len > 10_000)
