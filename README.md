# Errand Agent

A chat agent that arranges home-service errands (plumber, electrician, AC servicing, cleaning)
against a mocked provider world. It clarifies only what's missing, compares providers, asks for
approval before anything consequential, and recovers safely from tool failures.

The point of the design is that the safety properties are enforced **in code**, not by trusting
the model: approvals are deterministic and single-use, bookings are idempotent and verified at
the source of truth, injected text in tool output can't act, and memory hygiene is enforced by
the context builder.

## Quickstart (offline, no API key, ~2 minutes)

```bash
uv sync --all-extras
make check test eval
```

Windows without `make`: `uv run python tasks.py check test eval`.

- `make check` runs ruff (lint and format) and `mypy --strict` on `errand/`
- `make test` runs about 200 pytest tests, including every eval scenario and the mutation tests
- `make eval` regenerates [`evals/RESULTS.md`](evals/RESULTS.md) and [`evals/results.csv`](evals/results.csv) from a fresh run

## Run the demo UI

```bash
ERRAND_SCRIPT=demo make ui                             # keyless: scripted model for the demo phrases
ANTHROPIC_API_KEY=... make ui                          # live: claude-opus-5-5 (override with ERRAND_MODEL)
ERRAND_SCENARIO=timeout_after_success ERRAND_SCRIPT=demo make ui   # force a failure live
```

Then open http://localhost:8000. Chat is on the left and the live trace on the right. The
header buttons fire the provider-cancellation hook and a proactive watcher tick. CLI
alternative: `errand chat --scenario happy_path`.

## Architecture

```
user ─► Agent (orchestrator.py) ── per-session lease, top-level guard, limits
          │ 1. recover: reconcile PENDING intents via get_booking_status
          │ 2. approval pending? ─► approval.classify_confirmation (deterministic) ─► booking.execute
          │ 3. else NLU (LLM ─► NLUResult JSON, validated + 1 repair) ─► flows
          ▼
   search_flow / manage_flow / proactive ─► ToolGateway ─► MockToolEnv (8 tools, SQLite ledger)
          │                                   allowlist per state · retries · schema check · trace
          ├─ ReviewSummarizer (tool-less sub-agent, closed-enum output)
          ├─ MemoryStore + ContextBuilder (hard token budget + manifest)
          └─ AgentStore: sessions (versioned) · approvals · grants · booking_intents
```

| Package | What lives there |
|---|---|
| `errand/tools/` | fixed tool contracts, the mock world (`mock_env.py`), forced-outcome scenarios, `ToolGateway` |
| `errand/agent/` | state machine, approvals, booking and recovery, flows, untrusted-text handling, proactive watcher, templates |
| `errand/memory/` | memory store and commands, context builder, rolling summary |
| `errand/llm/` | `LLMClient` protocol, Anthropic adapter, scripted client, cassette record and replay |
| `errand/observability/` | tracer (JSONL, SSE listeners, cost) and redaction |
| `evals/` | harness, global invariants I1 to I6, 36 scenarios, seed data, report generator |

## Reproduce every deliverable

| Deliverable | Where / how |
|---|---|
| Implementation, mocks, tests | `errand/`, `tests/`; `make test` |
| Design note (1 page) | [`docs/DESIGN.md`](docs/DESIGN.md) |
| Eval table (generated) | `make eval` writes [`evals/RESULTS.md`](evals/RESULTS.md) and `results.csv` |
| Real bugs + regression tests | [`docs/BUGS.md`](docs/BUGS.md) |
| Timeout-after-success trace | [`traces/timeout_after_success.jsonl`](traces/timeout_after_success.jsonl); `make trace`; `errand trace show <file>` |
| Real-LLM cassettes | `ANTHROPIC_API_KEY=... make record` writes `evals/cassettes/<id>.jsonl`; `make eval` replays them offline |
| Demo script | [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) |
| Decisions log | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| AI-assistant transcripts | `docs/ai-transcripts/` |

## Configuration

Every limit, budget, probability and price lives in `errand/config.py` and can be overridden
with `ERRAND_*` environment variables (nested with `__`, e.g. `ERRAND_BOOK_ODDS__SUCCESS`).
Defaults: 8 model calls/turn, 25 tool calls/run, 60 s wall clock, 3,000-token context budget
with a 15% safety margin, 10-minute holds.
