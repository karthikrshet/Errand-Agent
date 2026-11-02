# Errand Agent: design note

```
user msg ─► orchestrator ─┬─ approval pending? ─► deterministic rules (no LLM) ─► booking.execute
                          └─ else ─► NLU (LLM proposes intent + SlotPatch) ─► code validates ─► flows
flows ─► ToolGateway (state allowlist, budget, retries, schema validation, trace) ─► mock provider (own ledger DB)
ReviewSummarizer: tool-less sub-agent, closed-enum output; the only model call that sees raw reviews
```

**1. State model.** An explicit enum + transition table (`agent/state.py`): IDLE → GATHERING → SEARCHING → PRESENTING_OPTIONS → HOLDING → AWAITING_APPROVAL → EXECUTING → VERIFYING → COMPLETED, plus CANCEL/RESCHEDULE/PROACTIVE flows and FAILED_SAFE/ABANDONED. Every transition is validated, traced with its *trigger*, and persisted before it takes effect (save-then-mutate), so a restart resumes exactly. EXECUTING and the cancel/reschedule flows only accept `user_message`/`approval_grant` triggers. **"LLM proposes, code disposes":** the model only turns text into a schema-validated `NLUResult`; code merges the `SlotPatch` (a correction touches only the fields it names; each field keeps provenance `user_*`/`memory_*`), ranks providers and runs tools. Less flexible than a free ReAct loop, but approvals and idempotency become provable properties instead of hopes.

**2. Approval enforcement.** Code builds an `ApprovalRequest` (action, provider, slot, price, hold/booking id, user, nonce); its SHA-256 fingerprint yields a letter-digit confirm code. The card is rendered from that object, never from LLM prose. Whether a reply approves is decided by a closed allowlist / code echo over the raw latest user message; anything with a negation, time, price, "but", "?" … is AMBIGUOUS and never grants (47-phrase table). A grant is minted, consumed and a PENDING intent written in **one** transaction, only while the request is `pending`; before that, the exact call is re-checked against the live hold (provider, slot, price). Any material change (a new slot, a re-hold quoting another price, a corrected field) invalidates the request and re-asks with a diff. Tool output, memories and summaries have no path to a grant (proved with a "gullible" model in evals 06/29/30).

**3. Idempotency and recovery.** The idempotency key = H(user, fingerprint), so repeated "yes", network retries and restarts reuse it. Write-ahead intent → `book`. On `ToolTimeout` the agent asks `get_booking_status` *first*: booked → success; not booked with a live hold → retry with the **same** key (bounded); expired → re-hold and fresh approval. Success is only claimed after a direct success or a verified status (invariant I3). On startup every PENDING intent is reconciled before new input; a restart never re-books without fresh approval. Guards live in SQLite: conditional updates under `BEGIN IMMEDIATE`, UNIQUE keys, a partial unique index on active `(provider, slot)` in the ledger. A per-session DB lease serialises turns and version-CAS rejects stale writes, but booking correctness never depends on either (8-thread race test).

**4. Memory and context.** Explicit facts are active at once; inferred ones ("prefers mornings") stay `pending_confirmation` and never reach a prompt until confirmed. Corrections supersede (history linked); *forget* marks every version forgotten and scrubs the value from memory rows **and** stored turns. `ContextBuilder` fills a hard budget (3,000 tokens × 0.85 margin, because tiktoken only approximates Claude's tokenizer; provider-reported counts are traced next to our estimate): state → pending approval → relevant memories → rolling summary → recent turns. Each call logs a manifest (tokens per section, memory IDs included/excluded with reason). The summariser is tool-less and schema-constrained; flagged items are dropped.

**5. Observability.** One JSONL trace per run: `run.start/end`, `state.transition` (with trigger), `model.call` (tokens, latency, cost from a price table, manifest), `tool.call` per attempt (redacted args, sanitised result, latency, outcome), `approval.*`, `security.injection_detected`, `memory.*`, `limit.hit`, `llm.fallback`. Six global invariants (I1–I6) are recomputed from the trace + both DBs after every eval; mutation tests prove they fail when the agent is broken.

**Trace excerpt** (`traces/timeout_after_success.jsonl`, eval 10):
```
26 approval.classified decision=affirm                       ← rules, not the LLM, read "yes"
27 approval.granted   grant=g_2d30… source=user_message
28 approval.consumed  idempotency_key=idem_a6c2…             ← same txn writes the PENDING intent
29 state.transition   AWAITING_APPROVAL→EXECUTING [approval_grant]
30 tool.call book     hold=h_000001 key=idem_a6c2… → error ToolTimeout   ← ledger was written anyway
31 state.transition   EXECUTING→VERIFYING [tool_result]      ← unknown outcome: do NOT retry yet
32 tool.call get_booking_status key=idem_a6c2… → ok          ← ask the source of truth first
33 booking.verified   booking_id=b_dfce8f1cd6
34 state.transition   VERIFYING→COMPLETED                    ← only now: "Booked and verified"
```

**6. Trade-offs and next steps.** SQLite + in-process mock keeps everything inspectable and offline; for multi-instance production: Postgres with the same constraints plus an outbox for provider calls, real per-call client timeouts, and user-level rate limits. Heuristic injection detection is an alarm, not the defence (the defence is structural). Next: LLM-judged evals over recorded real-model cassettes, more languages, a summary-quality eval.
