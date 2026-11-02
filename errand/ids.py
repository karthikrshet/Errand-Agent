"""Deterministic identifiers.

IDs come from a seeded RNG so that eval runs and traces are reproducible. The seed mixes in a
persisted boot counter, so a restarted process (same seed) never re-issues an ID from before
the restart.
"""

from __future__ import annotations

import random


class IdFactory:
    def __init__(self, seed: int, boot: int) -> None:
        self._rng = random.Random(f"ids:{seed}:{boot}")

    def new(self, prefix: str) -> str:
        return f"{prefix}_{self._rng.getrandbits(48):012x}"
