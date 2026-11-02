"""Seed data loader: 55 memories and a 160-turn prior conversation for u_demo."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from errand.memory.store import MemoryRecord
from evals.harness import EvalRun

HERE = Path(__file__).resolve().parent


def load_seed(run: EvalRun) -> None:
    memories = [
        MemoryRecord.model_validate(m)
        for m in json.loads((HERE / "memories.json").read_text(encoding="utf-8"))
    ]
    run.world.agent.memory.insert_raw(memories)
    run.tracer.emit("memory.status", changes={m.memory_id: m.status for m in memories})
    lines = (HERE / "conversation.jsonl").read_text(encoding="utf-8").splitlines()
    start = run.clock.now() - timedelta(days=30)
    for i, line in enumerate(lines):
        t = json.loads(line)
        run.world.store.add_turn(
            "history", "u_demo", t["role"], t["text"], start + timedelta(minutes=i)
        )
