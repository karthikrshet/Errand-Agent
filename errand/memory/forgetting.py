"""What "forget" has to reach beyond the memory table.

A forgotten fact can already have flowed elsewhere: into a request field (filled from memory),
into a rolling summary, or into stored conversation turns. Forgetting must remove it from every
place that can feed a prompt, otherwise it resurfaces (docs/BUGS.md #5).

Text scrubbing is whole-word and skips values that are too short or purely numeric ("10" for
an earliest hour): replacing those inside free text would corrupt unrelated content such as
prices and IDs, and they carry no identifying information.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from errand.agent.state import WorkflowState

SCRUBBED = "[forgotten]"
MIN_SCRUB_CHARS = 3

# Memory key -> request fields it can have filled.
KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "preferred_area": ("area",),
    "earliest_hour": ("earliest_hour",),
    "latest_hour": ("latest_hour",),
    "time_of_day": ("earliest_hour", "latest_hour"),
    "budget_ceiling": ("budget_paise",),
    "preferred_provider": ("favored_provider_id", "preferred_provider_id"),
    "blocked_provider": ("blocked_provider_ids",),
}


def scrubbable(values: Iterable[str]) -> list[str]:
    return [v for v in values if len(v) >= MIN_SCRUB_CHARS and not v.isdigit()]


def scrub_text(text: str, values: Iterable[str]) -> str:
    for v in scrubbable(values):
        text = re.sub(rf"(?<!\w){re.escape(v)}(?!\w)", SCRUBBED, text, flags=re.IGNORECASE)
    return text


def _matches(current: object, values: set[str]) -> bool:
    if current is None:
        return False
    candidates = {str(current)}
    if isinstance(current, int):
        candidates.add(str(current // 100))  # budget_paise vs a ceiling stored in rupees
    return bool(candidates & values)


def forget_in_state(state: WorkflowState, key: str, values: list[str]) -> list[str]:
    """Clear request fields derived from (or equal to) the forgotten fact; scrub the summary.

    Returns the request fields that were cleared.
    """
    forgotten = set(values)
    cleared: list[str] = []
    req = state.request
    for field in KEY_FIELDS.get(key, ()):
        current = getattr(req, field)
        if field == "blocked_provider_ids":
            kept = [p for p in current if p not in forgotten]
            if kept != current:
                req.blocked_provider_ids = kept
                cleared.append(field)
            continue
        from_memory = state.sources.get(field, "").startswith("memory")
        if current is not None and (from_memory or _matches(current, forgotten)):
            setattr(req, field, None)
            state.sources.pop(field, None)
            cleared.append(field)
    state.history_summary = scrub_text(state.history_summary, values)
    return cleared
