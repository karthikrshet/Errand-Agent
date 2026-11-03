# Decisions

One line each: what was chosen, and why.

- **Repo root = this folder** (not a nested `errand-agent/`): the brief's layout is applied at the top level so the repo is self-contained.
- **`tasks.py` behind the Makefile**: the dev machine is Windows without `make`; one Python task runner keeps `make X` and `python tasks.py X` identical.
- **Python 3.12 via `uv`**: satisfies 3.11+; `uv.lock` makes CI installs reproducible (`--frozen`).
- **`mypy --strict` on all of `errand/`** (not just the safety packages): cheaper to keep strict from day one than to tighten later.
- **`pydantic-settings` for `Settings`**: typed env overrides (`ERRAND_*`, nested via `__`) for every limit without bespoke parsing.
- **`Clock.sleep` on the protocol**: retry backoff goes through the clock so `FakeClock` makes retry-heavy evals instant and deterministic.
- **Default model `claude-opus-5-5`**, overridable with `ERRAND_MODEL`.
- **Two databases** (agent DB, provider ledger): the provider is a remote system in reality; keeping its ledger separate keeps "what we believe" and "what is true" honestly apart.
- **Business outcomes returned, transport failures raised** in tool contracts: `slot_taken`/`hold_expired`/`slot_conflict` must be handled; a timeout means "unknown", so it is an exception that forces a status check.
- **Approval classification is deterministic and LLM-free**; the model never sees a path to grant anything.
- **Grant mint + consume + intent in one transaction** (not issue-then-consume): no stranded grants (BUGS #4).
- **Per-session DB lease + version CAS**: lease serialises turns across processes; CAS turns any lease failure into a clean "handled elsewhere", never a lost update. Neither is relied on for booking correctness.
- **No automatic re-book after a restart**: if reconciliation finds "not booked", the user re-confirms against a fresh request.
- **Confirm codes are letter-digit-letter-digit** so they can never be read as a time or a price.
- **Read-step failures fall back to the last resting state** (keep the request); execution failures go to FAILED_SAFE with the intent left for reconciliation.
- **Review summariser output uses closed enums**: no review free text can reach the main agent or the user.
- **Forgetting scrubs conversation turns too**, not just the memory row, so a forgotten value cannot resurface via history or summaries.
- **Required fields are service, area, date window**; budget and time-of-day are optional constraints (asked about only implicitly via memory/defaults).
- **In-process mock tools**: per-tool timeouts are simulated by the mock raising `ToolTimeout`; a real HTTP adapter would enforce `tool_timeout_s` on the client.
- **Demo film generated from the real system** (`tools/video/`): Playwright drives the actual app, narration uses Kokoro, an open-source neural voice running locally (no text leaves the machine; the Windows voice is a fallback), and ffmpeg assembles it, so the video can be regenerated after any change.
- **Light, clean UI with Lucide icons** for the demo app and the film (the vanilla Lucide build, the same icon set as `lucide-react`; there's no React build step to justify), loaded from a version-pinned CDN URL with an SRI hash.
