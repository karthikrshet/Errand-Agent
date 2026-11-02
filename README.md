# Errand Agent

A chat-based agent that arranges home-service errands (plumber, electrician, AC servicing) against a
mocked provider world. Approvals, idempotency, injection resistance and memory hygiene are enforced
in application code, not by trusting the LLM.

> Status: under construction. Full quickstart, architecture diagram and demo instructions land in Phase 8.

## Quickstart (offline, no API key)

```bash
uv sync --all-extras
make check test eval
```

Windows without `make`: `uv run python tasks.py check test eval`.
