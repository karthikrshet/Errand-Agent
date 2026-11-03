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
- **Regression tests:** `tests/test_booking_safety.py::test_concurrent_double_confirmation_is_stable` (eval 27 fifteen times per test run; a 300-iteration stress loop was clean) and `::test_concurrent_grant_consumption_has_one_winner` (8 threads race the atomic grant with no lease).

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

## 7. A card the user had moved past could still be confirmed (found by independent review)

- **What broke:** with an approval card showing, the user wrote "it's for the bathroom tap actually". That re-ran the search. Then "yes" minted a grant and opened an intent for the *old* card. The move to EXECUTING then failed with `IllegalTransition`, so the user saw "Something went wrong", and the next turn reported an "interrupted booking". Nothing was booked (the transition table stopped it), but a grant was consumed for a card the user was no longer looking at, and all six invariants passed.
- **Why:** the approval was only invalidated when a *material* field changed (`job` isn't one). Leaving AWAITING_APPROVAL by any other route kept `pending_approval`, and the grant path didn't check which state the session was in.
- **Fix:**
  - `RunContext.move` invalidates the pending approval whenever the session leaves AWAITING_APPROVAL for anything other than EXECUTING. This is a single rule in one place.
  - `booking._check_request` refuses to mint a grant unless the session is in AWAITING_APPROVAL.
  - A bare "yes" with nothing to confirm now explains that.
  - New invariant **I7**: every grant was minted while its session showed that approval.
- **Regression tests:** eval `09b stale_approval_after_re_search`; `tests/test_invariants_have_teeth.py::test_i7_catches_grant_minted_outside_awaiting_approval` re-introduces the bug and expects I7 to fail.

## 8. A replacement promised mid-task was never delivered (found by independent review)

- **What broke:** a provider cancelled while the user was mid-booking. The watcher said "say 'find a replacement' and I'll look", but no intent handled that phrase, and later ticks skipped the booking. The replacement was never offered. The web "watcher" was also only a manual button.
- **Why:** the watcher marked the cancellation *seen* the moment it notified, so deferring was equivalent to dropping it.
- **Fix:** a mid-task detection is recorded as *notified* (told once) but not *seen*. The next tick after the user's workflow comes to rest runs the normal replacement flow, with fresh approval. The web app now runs a background watcher thread (`ERRAND_WATCH_INTERVAL_S`, default 15 s) whose messages reach the chat through an inbox, with no click needed.
- **Regression tests:** eval `18b proactive_cancellation_while_user_busy`; `tests/test_web.py::test_background_watcher_delivers_without_any_click`.

## 9. Any 400 from the provider silently disabled structured output (found on the first live run)

- **What broke:** on the first real-API run, every call returned `400 invalid_request_error: This API key is not scoped to a workspace…`. The agent degraded safely (bounded retries, then the deterministic fallback; nothing was booked). But the adapter had also flipped `_schema_supported = False` and retried without the JSON schema, as if the provider had rejected our schema.
- **Why:** the "schema not supported, retry with the schema in the prompt" fallback caught *every* `BadRequestError`. Auth, workspace or model-access errors would therefore permanently downgrade the client to unstructured output, hiding the real cause behind a second failure.
- **Fix:** the fallback now only triggers when the 400 is about the schema/output format. Other 400s surface as `LLMError` with the provider's message. The adapter also supports `ANTHROPIC_WORKSPACE_ID` (sent as `anthropic-workspace-id`), which user-level keys require.
- **Regression tests:** `tests/test_llm_adapters.py::test_non_schema_400_is_not_mistaken_for_schema_rejection`, `::test_workspace_id_is_sent_as_header`.

## 10. The web app shared one SQLite connection across threads (found by pre-submission review)

- **What broke:** three chat sessions plus the background watcher, driven at once against one `World`, hung until pytest's timeout. In the web app, FastAPI's threadpool and the watcher thread all used the same `sqlite3.Connection`. It was opened with `check_same_thread=False`, so nothing stopped one thread's statements from landing inside another thread's `BEGIN IMMEDIATE`.
- **Why:** the stores were written for one process with one thread per connection (the eval harness), and `check_same_thread=False` hid the mismatch instead of reporting it.
- **Fix:** `errand.db.ConnectionPool` gives each thread its own connection to the same database file, so transactions stay private and the file's locks do the serialising. An explicit connection or `:memory:` is still used as-is.
- **Regression test:** `tests/test_threads.py::test_concurrent_sessions_and_watcher_share_one_world` (hangs on the old code, about 8 s on the new).

## 11. A watcher tick on another tab declared an in-flight booking failed (found by pre-submission review)

- **What broke:** while tab A's `book` call was in flight, the background watcher ticked on tab B for the same user. Startup reconcile runs before every turn, so tab B asked `get_booking_status`, got `not_booked` (the call had not landed yet), marked the intent FAILED and told the user "Nothing was booked". A moment later tab A's booking succeeded.
- **Why:** reconcile treated every PENDING intent as abandoned. That is only true when no live turn owns it.
- **Fix:** `recover()` skips intents whose owning session currently holds a live lease. It emits `reconcile.skipped_in_flight`, and the owning turn settles the intent itself. Expired leases (a crashed worker) are still reconciled.
- **Regression test:** eval `36 other_tab_reconcile_during_book`.

## 12. A lost `book` response was reported as "Nothing was booked" (found by pre-submission review)

- **What broke:** the provider booked, then the response was garbled (`MalformedToolResponse`) or the connection dropped (`UnexpectedToolError`). The agent skipped the status check, went to FAILED_SAFE and said "Nothing was booked or changed". The ledger had a booking. This is a false claim of the opposite kind to I3's.
- **Why:** only `ToolTimeout` was treated as "outcome unknown". Every other error from the side-effect call fell through to the top-level guard, whose message assumes nothing happened.
- **Fix:**
  - `booking.OUTCOME_UNKNOWN` covers timeouts, malformed responses, unexpected transport errors and exhausted retries. For `book`, these go status-first. For cancel and reschedule, they leave the intent PENDING for the idempotent re-send.
  - The top-level guard now says "couldn't verify" instead of "nothing was booked" whenever the session has a PENDING intent.
- **Regression tests:** evals `35a book_response_lost_malformed` and `35b book_response_lost_conn_reset`.

## 13. A "yes" meant for another question approved a card the watcher had just pushed (found by pre-submission review)

- **What broke:** after a booking the agent asked "Want me to remember that you prefer mornings?". Meanwhile the background watcher pushed a replacement card for a provider cancellation into the chat. The user's "yes", meant for the memory question, booked the replacement.
- **Why:** a card raised without any user message is delivered asynchronously, so the user may answer something else before seeing it. The confirmation rules could not tell those two cases apart.
- **Fix:** an `ApprovalRequest` raised by the watcher is marked `unprompted` (display-only, outside the fingerprint). For such a card, only its own code (`CONFIRM K7P2`) grants; a bare "yes" gets `CODE_REQUIRED`, which keeps the card and asks for the code. Invariant I1 checks the same rule from the trace.
- **Regression tests:** eval `18c unprompted_card_needs_its_code` (books a second time on the old code); eval `18` now shows the bare "yes" being refused before the code confirms.

## 14. A model obeying an injected review could open a cancel card the user never asked for (found by pre-submission review)

- **What broke:** with a "gullible" model, "tell me more about option 1" came back as `cancel_booking` for the user's AC appointment, which put up a real CANCEL card. Approval was still required, but the user's next "ok" cancelled a real booking, and every invariant passed because the user had technically approved it.
- **Why:** the code only enforced *approval* for consequential actions. It did not enforce that the user had asked for the action in the first place, so the model's interpretation alone decided which card appeared.
- **Fix:** `agent/grounding.py` requires a matching verb in the user's raw message (cancel, call off, move, reschedule, postpone, …) before a cancel or reschedule flow can start. Otherwise the agent says it changed nothing and traces `security.ungrounded_intent`. I6 now has an independent, broader word oracle for entering those flows.
- **Regression tests:** eval `06b gullible_cancel_then_ok`; `tests/test_grounding.py`; `tests/test_invariants_have_teeth.py::test_i6_catches_cancel_flow_from_ungrounded_text`.
