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

## 4. The fix for #2 introduced a liveness bug under concurrency (found by stress-running eval 27)

- **What broke:** eval 27 became flaky (about 1 run in 20; 11/200 in a stress loop). In failing runs *nothing* was booked, the approval sat in `granted` with no intent behind it (so every later "yes" was refused), and some runs leaked a stack trace from a worker thread.
- **Why:** three interacting problems. (a) Grants were issued in one transaction and consumed in another, so a grant could be left issued-but-unused. (b) With optimistic versioning, the *losing* process, which had nothing to do, often saved first; the winner, already holding a fresh grant, then hit `StaleState` and aborted, stranding the grant. (c) `move()` mutated the in-memory state before saving, and the error handlers themselves saved again, so a second `StaleState` escaped the top-level guard.
- **Fix:** removed the classes of problem instead of patching interleavings: the grant is now minted, consumed and the PENDING intent written in **one** transaction (`AgentStore.grant_and_open_intent`), so a stranded grant cannot exist; turns on a session are serialised across processes by a DB **lease** (released in `finally`, TTL frees a crashed holder); version CAS stays as the backstop; `move()` is save-then-mutate and every handler is safe against `StaleState`. Booking correctness still never depends on the lease.
- **Regression tests:** `tests/test_booking_safety.py::test_concurrent_double_confirmation_is_stable` (eval 27 thirty times per test run; a 300-iteration stress loop was clean) and `::test_concurrent_grant_consumption_has_one_winner` (8 threads race the atomic grant with no lease).

## 5. A forgotten fact still reached the prompt through the live request (found by independent review)

- **What broke:** "I'm in Koramangala, remember that" → "find a plumber" (area filled from memory) → "forget my area" → the next NLU prompt still contained `request: {"area": "Koramangala"}` inside `<workflow_state>`, and later searches kept using it. Invariant I4 caught it once the scenario existed; eval 21 never exercised this path because the key it forgot was never applied to a request.
- **Why:** `forget` scrubbed the memory rows, turns and the current session's summary, but not the *derived copies*: request fields filled from that memory, or other sessions' state. A second issue in the same code: turns were scrubbed with a raw substring replace, so forgetting `earliest_hour=10` turned "1050 rupees" into "[forgotten]50".
- **Fix:** `memory/forgetting.py`. On forget, clear every request field filled from that memory, or equal to its value, in every session of the user, and scrub every session summary. Scrub turns by whole word only, skipping short or purely numeric values, which can't be scrubbed from free text safely and identify nothing.
- **Regression tests:** eval `21b memory_forget_after_value_used` (under I4), `tests/test_memory_forgetting.py`.

## 6. The "hard" context budget could be exceeded (found by independent review)

- **What broke:** a long user message produced a prompt of 3,944 tokens against an effective budget of 2,550, and invariant I4 fired.
- **Why:** `ContextBuilder` capped only the optional sections (memories, summary, turns). The user message, the workflow state and the NLU's own wrapper (`TODAY:`, `USER MESSAGE:`) were charged without any cap, and repair rounds and the review summariser weren't budgeted at all. "Hard budget" was only true for typical inputs.
- **Fix:**
  - The builder now receives the exact system prompt and produces the exact user body.
  - It gives the user message and the state capped shares (35% and 30%, truncating or compacting beyond that).
  - It counts the joined prompt exactly and drops history until it fits.
  - A repair round that would exceed the budget falls back instead.
  - The review summariser keeps only the reviews that fit.
- **Regression tests:** `tests/test_context_budget.py` (exact-count property over adversarial sizes) and eval `23b context_budget_oversized_message` under I4.
