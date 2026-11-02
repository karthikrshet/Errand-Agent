# Decisions

One line each: what was chosen, and why.

- **Repo root = this folder** (not a nested `errand-agent/`): the brief's layout is applied at the top level so the repo is self-contained.
- **`tasks.py` behind the Makefile**: the dev machine is Windows without `make`; one Python task runner keeps `make X` and `python tasks.py X` identical.
- **Python 3.12 via `uv`**: satisfies 3.11+; `uv.lock` makes CI installs reproducible (`--frozen`).
- **`mypy --strict` on all of `errand/`** (not just the safety packages): cheaper to keep strict from day one than to tighten later.
- **`pydantic-settings` for `Settings`**: typed env overrides (`ERRAND_*`, nested via `__`) for every limit without bespoke parsing.
- **`Clock.sleep` on the protocol**: retry backoff goes through the clock so `FakeClock` makes retry-heavy evals instant and deterministic.
- **Default model `claude-opus-5-5`**, overridable with `ERRAND_MODEL`.
