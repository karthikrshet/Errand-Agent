# Real bugs hit while building

Each entry: what broke, why, the fix, and the regression test that now guards it. Only real bugs.

## 1. A field named `date` silently broke its own type annotation

- **What broke:** importing the agent failed with `TypeError: unsupported operand type(s) for |: 'NoneType' and 'NoneType'` while pydantic built `RescheduleTo`.
- **Why:** the model declared `date: date | None = None`. With `from __future__ import annotations` the annotation is a string evaluated later *in the class namespace*, where `date` now refers to the field's default (`None`), so it became `None | None`. mypy did not flag it because it resolves the name statically.
- **Fix:** import the module (`import datetime as dt`) and annotate `date: dt.date | None`; keep the field name `date` because it is what the LLM-facing schema should say.
- **Regression test:** `tests/test_nlu.py::test_reschedule_to_parses_date_field`.

## 2. Lost update: a losing concurrent confirmation overwrote the winner's session state

- **What broke:** eval 27 (two processes confirm the same approval at the same instant). Bookings were correct (exactly one grant, one intent, one booking), but the session ended in `AWAITING_APPROVAL` with a pending approval that the DB already marked `granted`, instead of `COMPLETED`.
- **Why:** each process loaded the session JSON, worked, and saved it with an unconditional upsert. The loser (whose grant was refused by the conditional UPDATE) saved its stale snapshot *after* the winner, so last-writer-wins regressed the state. The booking guards were in the database; session state was not.
- **Fix:** optimistic concurrency. `WorkflowState.version` is a compare-and-swap token; `AgentStore.save_state` updates `WHERE version = ?` and raises `StaleState` otherwise. The orchestrator then adopts the newer state and tells the user it was handled elsewhere (`session.stale_write_rejected` in the trace). A refused-but-affirmative confirmation now replies "already received" instead of falling through to NLU.
- **Regression tests:** `tests/test_booking_safety.py::test_stale_session_write_is_rejected`, and eval `27 concurrent_double_confirmation` now asserts the final state is `COMPLETED`.

## 3. After a restart, success was reported from our own record without re-checking the provider

- **What broke:** eval 28c (crash at `after_booking_before_state_save`) failed invariant I3: on restart the agent said "it went through and is verified" in a run that contained no verification against the source of truth.
- **Why:** the recovery path saw the intent marked `SUCCEEDED` in the agent DB and reported it directly. That record was written after a verified response, but the run that tells the user must itself be backed by the provider's ledger; trusting local state is exactly the shortcut the brief forbids ("never claim success until verified").
- **Fix:** `booking._reverify` calls `get_booking_status` for a session stuck mid-execution with a `SUCCEEDED` intent before saying anything; on disagreement it reports "unverified" and goes to `FAILED_SAFE`.
- **Regression tests:** `tests/test_booking_safety.py::test_restart_reverifies_a_succeeded_intent_before_claiming`, and eval `28c` under invariant I3.
