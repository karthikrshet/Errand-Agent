"""Record real-model cassettes for selected scenarios: `make record` (needs ANTHROPIC_API_KEY).

Each scenario runs once against the live Anthropic model (its scripted model outputs are
ignored); every model response is written to evals/cassettes/<scenario id>.jsonl. `make eval`
then replays those cassettes offline as extra rows, so the table shows the system working with
real model output, not only hand-scripted output.
"""

from __future__ import annotations

import sys
from pathlib import Path

from errand.config import Settings
from errand.llm.anthropic_client import AnthropicClient
from errand.llm.cassette import RecordingClient
from evals.harness import execute
from evals.invariants import check_all
from evals.scenarios import all_scenarios

ROOT = Path(__file__).resolve().parent
DEFAULT = ["01", "04", "05"]


def main(argv: list[str]) -> int:
    wanted = argv or DEFAULT
    ok = True
    for sc in (s for s in all_scenarios() if s.sid in wanted):
        path = ROOT / "cassettes" / f"{sc.sid}.jsonl"
        path.unlink(missing_ok=True)
        run = execute(sc, ROOT / ".runs" / f"record-{sc.sid}",
                      RecordingClient(AnthropicClient(Settings()), path))  # fmt: skip
        failures = sc.check(run) + [m for v in check_all(run).values() for m in v]
        ok &= not failures
        print(f"{'PASS' if not failures else 'FAIL'} {sc.sid} {sc.name} -> {path}")
        for f in failures:
            print(f"   - {f}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
