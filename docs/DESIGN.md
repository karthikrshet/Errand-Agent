# Errand Agent: design note

```
user msg ─► orchestrator ─┬─ approval pending? ─► deterministic rules (no LLM) ─► booking.execute
                          └─ else ─► NLU (LLM proposes intent + SlotPatch) ─► code validates ─► flows
flows ─► ToolGateway (state allowlist, budget, retries, schema check, trace) ─► mock provider (own ledger)
ReviewSummarizer: tool-less sub-agent, closed-enum output; the only model call that sees raw reviews
```

**1. State model.** An explicit enum and transition table (`agent/state.py`), from IDLE through HOLDING, AWAITING_APPROVAL, EXECUTING and VERIFYING to COMPLETED, plus cancel, reschedule and proactive flows and FAILED_SAFE/ABANDONED. Every transition is validated, traced with its trigger and persisted before it takes effect, so a restart resumes exactly. **LLM proposes, code disposes:** the model only turns text into a schema-validated intent and `SlotPatch`. Code merges it (a correction touches only the fields it names), ranks providers and calls tools. This is less flexible than a free ReAct loop, but approvals and idempotency become provable.

**2. Approval enforcement.** Code builds the `ApprovalRequest` (action, provider, slot, price, hold or booking, user, nonce). Its SHA-256 fingerprint gives a confirm code, and the card is rendered from that object. A closed allowlist over the raw latest message decides approval. It knows the card's action, and anything with a negation, time, price, "but" or "?" is ambiguous and never grants. Cards the watcher raises unprompted need their code. Mint, consume and a PENDING intent happen in **one** transaction, only while the request is pending and the live hold still matches. Any material change invalidates the card and re-asks with a diff. Cancel and reschedule must also be *grounded*: a matching verb in the user's own words. Tool output, memories and summaries have no path to a grant.

**3. Idempotency and recovery.** Idempotency key = H(user, fingerprint), so repeated "yes", retries and restarts reuse it. The intent is written ahead of `book`. On any outcome-unknown error (timeout, garbled reply, dropped connection), the agent asks `get_booking_status` first. If booked, it reports success. If not booked and the hold is live, it retries with the same key, bounded. If the hold expired, it re-holds and asks for fresh approval. "Booked" is only said after a verified status (I3). Before new input, PENDING intents are reconciled, except those a live turn in another tab still owns. Guards live in SQLite: a compare-and-swap under `BEGIN IMMEDIATE`, UNIQUE keys, and a partial unique index on active `(provider, slot)`. A per-session lease and version checks serialise turns, but correctness never depends on them.

**4. Memory and context.** Explicit facts are active at once. Inferred ones stay `pending_confirmation` and never reach a prompt until confirmed. Corrections supersede. *Forget* scrubs the value from memory rows, summaries and turns, clears derived request fields in every session, and removes the turn that stated it. `ContextBuilder` enforces a hard budget on the exact prompt: 3,000 tokens × 0.85, because tiktoken only approximates Claude's tokenizer, and provider counts are traced alongside. Every call logs a manifest of tokens per section and memory IDs included or excluded, with the reason.

**5. Observability and evals.** One JSONL trace per run covers transitions, `model.call` (tokens, latency, cost), `tool.call` per attempt, `approval.*`, `security.*`, `memory.*`, `limit.hit` and `llm.fallback`, with redaction. Seven invariants (I1–I7) are recomputed from the trace and both databases after every eval, and mutation tests prove they can fail. BUGS.md lists 15 real defects, each with a regression test.

**Trace excerpt** (`traces/timeout_after_success.jsonl`, eval 10):
```
26 approval.classified decision=affirm                       ← rules, not the LLM, read "yes"
28 approval.consumed  idempotency_key=idem_a6c2…             ← same txn writes the PENDING intent
29 state.transition   AWAITING_APPROVAL→EXECUTING [approval_grant]
30 tool.call book     key=idem_a6c2… → error ToolTimeout      ← the ledger was written anyway
31 state.transition   EXECUTING→VERIFYING [tool_result]      ← outcome unknown: do NOT retry yet
32 tool.call get_booking_status key=idem_a6c2… → ok          ← ask the source of truth first
33 booking.verified   booking_id=b_dfce8f1cd6
34 state.transition   VERIFYING→COMPLETED                    ← only now: "Booked and verified"
```

**6. Trade-offs and next steps.** SQLite and an in-process mock keep everything inspectable and offline. Multi-instance production would need Postgres with the same constraints, an outbox for provider calls, real client timeouts and per-user rate limits. Injection *detection* is a heuristic alarm; the defence is structural. Traces are append-only debug logs, so forgetting does not rewrite them; production needs a retention policy. Next steps: LLM-judged evals over recorded cassettes, and a summary-quality eval.
