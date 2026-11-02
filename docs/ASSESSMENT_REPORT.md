# Assessment report

An independent, read-only review was run as the hiring company's CTO would run it. The
reviewer read the assignment, read the code, and wrote adversarial scripts to break the
system. The reviewer had no access to the author's reasoning. This report records what that
review found, what was fixed, how each fix was verified, and what is still open.

## Review verdict before fixes

**7/10: "lean hire, conditional on a live-LLM walkthrough."** The reviewer reproduced 4 of 5
adversarial attempts.

The reviewer's strengths:
- The safety core is genuinely strong: deterministic confirmation, an atomic grant-plus-intent
  transaction, status-first timeout handling, and a restart crash-point matrix.
- The engineering is honest: real concurrency bugs are documented with root causes, invariants
  are recomputed from the DB, ledger and trace, and mutation tests check the invariants.
- The code is clean: strict mypy, small modules, and typed tool boundaries.

The reviewer's weaknesses:
- There is no evidence of a real-LLM run.
- The memory and context guarantees were overstated: the forget leak and the soft budget
  were both reproducible.
- The evals leaned on happy-path fixtures, so some state-consistency bugs passed every
  invariant.

## Findings and status

| # | Severity | Finding | Status | Verification |
|---|---|---|---|---|
| 1 | Major | A forgotten fact still reached the prompt via `request` fields in `<workflow_state>` | **Fixed** (BUGS #5) | reviewer repro re-run clean; eval `21b`; I4 |
| 2 | Major | The "hard" context budget could be exceeded (3,944 > 2,550 tokens) | **Fixed** (BUGS #6) | repro now 1,249 tokens; property test over adversarial sizes; eval `23b` |
| 3 | Major | A stale approval card stayed confirmable after a re-search; the grant was consumed | **Fixed** (BUGS #7) | repro re-run: no grant, no intent; eval `09b`; new invariant **I7** + mutation test |
| 4 | Major | No evidence a real LLM was ever used | **Open** | needs the owner's API key: `make record` (see below) |
| 5 | Major | A replacement promised mid-task was never delivered; the watcher was only a button | **Fixed** (BUGS #8) | eval `18b`; background watcher thread + web test with no click |
| 6 | Minor | `forget` over-scrubbed ("1050 rupees" became "[forgotten]50") | **Fixed** (BUGS #5) | repro re-run; `tests/test_memory_forgetting.py` |
| 7 | Minor | Eval blind spots: the forget path never applied to a request; I6 was tautological; all booking evals forced their outcomes | **Fixed** | eval `21b`; I6 now checks which run entered reserved states; eval `34` + a 40-seed test on the real 70/15/15 odds (hits success, `slot_taken` and both timeout paths) |
| 7b | Minor | A saved technician preference never shaped the search | **Fixed** | it now ranks the provider higher (soft preference, never a filter) |
| 8 | Minor | Cancel/reschedule did not re-check the target booking before acting | **Fixed** | eval `17b`: the provider cancels meanwhile; no cancel is sent and there is no false "Cancelled and verified" |
| 9 | Minor | "yes but 11am" dropped the requested change | **Fixed** | eval `33b`: never grants, acts on the change, books 11:00 only after an explicit "yes" |
| 10 | Nit | Unused error types, a redundant `except`, a dead `None` check | **Fixed** | removed |
| 10b | Nit | Trace JSONL keeps raw user text, including values later forgotten | **Accepted** | traces are operator logs, not model context; they would need their own retention policy |

## Requirement coverage (against the assignment)

| Requirement | Status |
|---|---|
| 8 tool contracts and failure modes | Met |
| Conversation and explicit state | Met |
| Approval enforced by code; invalidated on material change | Met (stale-card gap closed; I7 added) |
| Bounded execution | Met |
| Booking correctness, idempotency, restart | Met |
| Untrusted tool output / injection | Met |
| Memory: inspect, correct, forget; explicit vs inferred | Met (forget leak closed) |
| Observability | Met for schema; real token and cost data waits on a live run |
| 20+ evals asserting outcomes and invariants | Met: 43 scenarios, 7 invariants, mutation-tested |
| Proactive follow-up | Met (deferred path and background watcher added) |
| Context management | Met (hard budget on the exact prompt) |
| Real LLM drives the agent | **Open:** the adapter is built and offline-tested, but has not been run live |
| Design note, bugs, trace | Met: 8 real bugs, each with an existing regression test |
| Demo video, AI transcripts | **Open:** the owner records and exports these |

## Current state (verified)

- `make check test eval` passes offline: ruff, `mypy --strict`, about 215 tests, and 43/43 scenarios.
- GitHub Actions CI passed on Linux.
- The evals ran 300/300 in a stress loop on the concurrency scenario.

## What is still open, and how to close it

1. **Real-LLM evidence (the most important gap).** With a key:
   ```bash
   ANTHROPIC_API_KEY=sk-ant-... uv run python tasks.py record   # scenarios 01, 04, 05
   uv run python tasks.py eval                                   # adds offline "-rec" rows
   ```
   Commit `evals/cassettes/*.jsonl` and the regenerated `evals/RESULTS.md`. A live
   `make ui` session also leaves a trace in `traces/` with real token counts and cost.
2. **Demo video:** follow [`docs/DEMO_SCRIPT.md`](DEMO_SCRIPT.md).
3. **AI-assistant transcripts:** export this session into `docs/ai-transcripts/`.

The "after" state has not been independently re-scored. A fresh review of the current commit
would be the honest way to get one.
