"""User-facing memory operations: inspect, remember, correct, confirm, forget; plus applying
active memories as defaults and inferring (unconfirmed) preferences from booking history."""

from __future__ import annotations

from collections import Counter

from errand.agent import responses as r
from errand.agent.nlu import MemoryOp
from errand.agent.runtime import RunContext
from errand.domain.money import format_inr
from errand.domain.request import FieldSource, SlotPatch
from errand.domain.time import to_ist
from errand.memory.store import MemoryRecord
from errand.tools import contracts as c

LABELS = {
    "preferred_area": "area",
    "earliest_hour": "earliest time",
    "latest_hour": "latest time",
    "time_of_day": "time of day",
    "preferred_provider": "technician/provider preference",
    "blocked_provider": "provider to avoid",
    "budget_ceiling": "budget ceiling",
    "language": "language",
    "other": "note",
}
MORNING_BOOKINGS_TO_INFER = 3


def _statuses(ctx: RunContext, changes: dict[str, str]) -> None:
    """Trace every status change, so audits can replay what was allowed into each prompt."""
    ctx.tracer.emit("memory.status", changes=changes)


def _describe(m: MemoryRecord) -> str:
    label = LABELS.get(m.key, m.key.replace("_", " "))
    scope = f" ({r.service_name(m.service)})" if m.service else ""
    value = m.value
    if m.key in ("earliest_hour", "latest_hour") and value.isdigit():
        value = f"{int(value)}:00"
    if m.key == "budget_ceiling" and value.isdigit():
        value = format_inr(int(value) * 100)
    return f"- {label}{scope}: {value}"


def show(ctx: RunContext) -> None:
    records = ctx.memory.records(ctx.user_id)
    explicit = [m for m in records if m.status == "active" and m.kind == "explicit"]
    confirmed = [m for m in records if m.status == "active" and m.kind == "inferred"]
    pending = [m for m in records if m.status == "pending_confirmation"]
    ctx.tracer.emit("memory.read", purpose="inspect", ids=[m.memory_id for m in records])
    if not (explicit or confirmed or pending):
        ctx.say("I don't have anything stored about you yet.")
        return
    parts = []
    if explicit:
        parts.append("Things you told me:\n" + "\n".join(_describe(m) for m in explicit))
    if confirmed:
        parts.append("Things I noticed and you confirmed:\n"
                     + "\n".join(_describe(m) for m in confirmed))  # fmt: skip
    if pending:
        parts.append(
            "Things I noticed but won't use until you confirm:\n"
            + "\n".join(_describe(m) for m in pending)
        )
    parts.append("Say 'actually my area is ...' to correct something, or 'forget my ...'.")
    ctx.say("\n\n".join(parts))


def update(ctx: RunContext, op: MemoryOp) -> None:
    """Correct or confirm a stored fact. Confirming an inferred one moves it into use."""
    pending = next(
        (m for m in ctx.memory.pending(ctx.user_id)
         if m.key == op.key and (op.value is None or op.value == m.value)),
        None,
    )  # fmt: skip
    if pending is not None:
        ctx.memory.confirm(ctx.user_id, pending.memory_id, ctx.clock.now())
        ctx.tracer.emit("memory.write", op="confirm", id=pending.memory_id, key=pending.key)
        _statuses(ctx, {pending.memory_id: "active"})
        ctx.say(f"Thanks, I'll remember that:\n{_describe(pending)}")
        return
    if op.value is None:
        ctx.say("What should I change it to?")
        return
    rec = ctx.memory.remember(
        ctx.user_id, op.key, op.value, memory_id=ctx.ids.new("m"), now=ctx.clock.now(),
        service=op.service, evidence=f"turn:{ctx.user_turn_id}",
    )  # fmt: skip
    _statuses(ctx, _upsert_changes(rec))
    ctx.tracer.emit("memory.write", op="upsert", id=rec.memory_id, key=rec.key,
                    supersedes=rec.supersedes_id)  # fmt: skip
    ctx.say(f"Updated:\n{_describe(rec)}")


def forget(ctx: RunContext, op: MemoryOp) -> None:
    ids = [m.memory_id for m in ctx.memory.records(ctx.user_id) if m.key == op.key]
    values = ctx.memory.forget(ctx.user_id, op.key, ctx.clock.now())
    _statuses(ctx, dict.fromkeys(ids, "forgotten"))
    scrubbed_turns = ctx.store.scrub_turns(ctx.user_id, values)
    summary = ctx.state.history_summary
    for v in values:
        summary = summary.replace(v, "[forgotten]")
    ctx.state.history_summary = summary
    ctx.tracer.emit("memory.forget", key=op.key, versions=len(values),
                    turns_scrubbed=scrubbed_turns)  # fmt: skip
    label = LABELS.get(op.key, op.key.replace("_", " "))
    if values:
        ctx.say(f"Done. I've forgotten your {label} and removed it from our history.")
    else:
        ctx.say(f"I didn't have a {label} stored.")


def _upsert_changes(rec: MemoryRecord) -> dict[str, str]:
    changes = {rec.memory_id: "active"}
    if rec.supersedes_id:
        changes[rec.supersedes_id] = "superseded"
    return changes


PATCH_TO_MEMORY = {
    "area": "preferred_area",
    "earliest_hour": "earliest_hour",
    "latest_hour": "latest_hour",
    "preferred_provider_id": "preferred_provider",
    "block_provider_id": "blocked_provider",
    "budget_rupees": "budget_ceiling",
}


def remember_from_patch(ctx: RunContext, patch: SlotPatch, service: str | None) -> None:
    stored = []
    for field, key in PATCH_TO_MEMORY.items():
        value = getattr(patch, field)
        if value is None:
            continue
        scope = service if key in ("budget_ceiling", "preferred_provider") else None
        rec = ctx.memory.remember(
            ctx.user_id, key, str(value), memory_id=ctx.ids.new("m"), now=ctx.clock.now(),
            service=scope, evidence=f"turn:{ctx.user_turn_id}",
        )  # fmt: skip
        ctx.tracer.emit("memory.write", op="remember", id=rec.memory_id, key=key)
        _statuses(ctx, _upsert_changes(rec))
        stored.append(_describe(rec))
    if stored:
        ctx.say("I'll remember:\n" + "\n".join(stored))


def apply_defaults(ctx: RunContext) -> list[str]:
    """Fill fields the user hasn't given from active memories (explicit or confirmed)."""
    st = ctx.state
    req = st.request
    used: list[str] = []
    for m in ctx.memory.active(ctx.user_id):
        if m.service is not None and m.service != req.service:
            continue
        source: FieldSource = "memory_explicit" if m.kind == "explicit" else "memory_confirmed"
        for field, value in _as_request_fields(m).items():
            current = getattr(req, field)
            if field == "blocked_provider_ids":
                if value not in current:
                    req.blocked_provider_ids = [*current, str(value)]
                    used.append(m.memory_id)
                continue
            if current is None:
                setattr(req, field, value)
                st.sources[field] = source
                used.append(m.memory_id)
    if used:
        ctx.tracer.emit("memory.read", purpose="defaults", ids=sorted(set(used)))
    return used


def _as_request_fields(m: MemoryRecord) -> dict[str, object]:
    v = m.value
    if m.key == "preferred_area":
        return {"area": v}
    if m.key in ("earliest_hour", "latest_hour") and v.isdigit():
        return {m.key: int(v)}
    if m.key == "time_of_day" and v == "morning":
        return {"earliest_hour": 8, "latest_hour": 12}
    if m.key == "budget_ceiling" and v.isdigit():
        return {"budget_paise": int(v) * 100}
    if m.key == "blocked_provider":
        return {"blocked_provider_ids": v}
    return {}


def infer_preferences(ctx: RunContext) -> None:
    """After a booking: if most bookings are mornings, propose (not store as fact) a preference."""
    out = ctx.tool("list_bookings", c.ListBookingsInput(user_id=ctx.user_id), c.ListBookingsOutput)
    hours = Counter(
        "morning" if to_ist(b.slot).hour < 12 else "later"
        for b in out.bookings
        if b.status in ("completed", "upcoming")
    )
    if hours["morning"] < MORNING_BOOKINGS_TO_INFER or hours["morning"] <= hours["later"]:
        return
    rec = ctx.memory.infer(
        ctx.user_id, "time_of_day", "morning", memory_id=ctx.ids.new("m"), now=ctx.clock.now(),
        confidence=round(hours["morning"] / sum(hours.values()), 2),
        evidence=f"{hours['morning']} morning bookings",
    )  # fmt: skip
    if rec is not None:
        _statuses(ctx, {rec.memory_id: "pending_confirmation"})
        ctx.tracer.emit("memory.write", op="infer", id=rec.memory_id, key=rec.key,
                        status=rec.status)  # fmt: skip
        ctx.say(
            "I noticed most of your bookings are in the morning. Want me to remember that you "
            "prefer mornings? Say 'yes, remember mornings'. I won't use it until you confirm."
        )
