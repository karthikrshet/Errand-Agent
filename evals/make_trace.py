"""`make trace`: regenerate traces/timeout_after_success.jsonl from eval scenario 10."""

from __future__ import annotations

import shutil
from pathlib import Path

from evals.harness import execute
from evals.invariants import check_all
from evals.scenarios import all_scenarios

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sc = next(s for s in all_scenarios() if s.name == "timeout_after_success")
    run = execute(sc, ROOT / "evals" / ".runs" / "trace-timeout")
    failures = sc.check(run) + [m for v in check_all(run).values() for m in v]
    out = ROOT / "traces" / "timeout_after_success.jsonl"
    out.parent.mkdir(exist_ok=True)
    shutil.copyfile(run.workdir / "trace.jsonl", out)
    status = failures or "ok"
    print(f"wrote {out} ({len(run.tracer.events)} events); checks: {status}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
