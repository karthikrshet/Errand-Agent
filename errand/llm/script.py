"""Helpers for writing scripted model outputs in tests and eval scenarios."""

from __future__ import annotations

import json
from typing import Any


def nlu(intent: str, **fields: Any) -> str:
    """Build the raw JSON a model would return. Unknown top-level keys go into `patch`."""
    top = {"selection", "booking_ref", "reschedule_to", "memory", "remember"}
    out: dict[str, Any] = {"intent": intent}
    patch = {k: v for k, v in fields.items() if k not in top}
    if patch:
        out["patch"] = patch
    out.update({k: v for k, v in fields.items() if k in top})
    return json.dumps(out, default=str)


def select(n: int, hour: int | None = None) -> str:
    sel: dict[str, int] = {"option_number": n}
    if hour is not None:
        sel["hour"] = hour
    return nlu("select_option", selection=sel)
