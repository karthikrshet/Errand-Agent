"""The user's service request, with provenance for every field.

`SlotPatch` is what the LLM proposes; `merge_patch` is the deterministic code that applies it.
A correction only touches the fields present in the patch, so unrelated context is preserved by
construction rather than by asking the model to remember it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FieldSource = Literal["user_this_turn", "user_earlier", "memory_explicit", "memory_confirmed"]

REQUIRED_FIELDS: tuple[str, ...] = ("service", "area", "date_start")
MATERIAL_FIELDS: frozenset[str] = frozenset(
    {
        "service",
        "area",
        "date_start",
        "date_end",
        "earliest_hour",
        "latest_hour",
        "budget_paise",
        "preferred_provider_id",
        "blocked_provider_ids",
    }
)


class ServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str | None = None
    job: str | None = None
    area: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    earliest_hour: int | None = Field(default=None, ge=0, le=23)
    latest_hour: int | None = Field(default=None, ge=0, le=23)
    budget_paise: int | None = Field(default=None, ge=0)
    preferred_provider_id: str | None = None
    blocked_provider_ids: list[str] = Field(default_factory=list)
    same_provider_as_last: bool = False

    def missing(self) -> list[str]:
        return [f for f in REQUIRED_FIELDS if getattr(self, f) is None]


class SlotPatch(BaseModel):
    """Fields the user stated (or corrected) this turn. `None` means 'not mentioned'."""

    model_config = ConfigDict(extra="forbid")

    service: str | None = None
    job: str | None = None
    area: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    earliest_hour: int | None = Field(default=None, ge=0, le=23)
    latest_hour: int | None = Field(default=None, ge=0, le=23)
    budget_rupees: int | None = Field(default=None, ge=0)
    preferred_provider_id: str | None = None
    block_provider_id: str | None = None
    same_provider_as_last: bool | None = None

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)


class MergeResult(BaseModel):
    request: ServiceRequest
    sources: dict[str, FieldSource]
    changed: list[str]


def age_sources(sources: dict[str, FieldSource]) -> dict[str, FieldSource]:
    """At the start of a turn, last turn's statements become 'earlier' statements."""
    return {k: ("user_earlier" if v == "user_this_turn" else v) for k, v in sources.items()}


def merge_patch(
    req: ServiceRequest, sources: dict[str, FieldSource], patch: SlotPatch
) -> MergeResult:
    updates: dict[str, object] = {}
    for name, value in patch.model_dump(exclude_none=True).items():
        if name == "budget_rupees":
            updates["budget_paise"] = int(value) * 100
        elif name == "block_provider_id":
            if value not in req.blocked_provider_ids:
                updates["blocked_provider_ids"] = [*req.blocked_provider_ids, value]
        else:
            updates[name] = value
    if "date_start" in updates and "date_end" not in updates:
        start = updates["date_start"]
        if req.date_end is not None and isinstance(start, date) and req.date_end < start:
            updates["date_end"] = start
    changed = [k for k, v in updates.items() if getattr(req, k) != v]
    new_sources = dict(sources)
    for k in changed:
        new_sources[k] = "user_this_turn"
    merged = req.model_copy(update=updates)
    return MergeResult(
        request=ServiceRequest.model_validate(merged.model_dump()),
        sources=new_sources,
        changed=changed,
    )
