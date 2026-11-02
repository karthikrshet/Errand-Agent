"""Booking flow up to the approval card: gather -> search -> present -> select -> hold -> ask."""

from __future__ import annotations

from datetime import datetime

from errand.agent import responses as r
from errand.agent.approval_flow import invalidate_pending, request_approval
from errand.agent.nlu import NLUResult, Selection
from errand.agent.ranking import rank
from errand.agent.review_summarizer import summarize_reviews
from errand.agent.runtime import RunContext
from errand.agent.state import Candidate, HoldInfo, State
from errand.agent.untrusted import sanitize
from errand.domain.request import MATERIAL_FIELDS, merge_patch
from errand.domain.time import to_ist
from errand.errors import InvalidRequest
from errand.memory import commands as memory_cmd
from errand.tools import contracts as c
from errand.tools.catalog import SERVICES


def apply_patch(ctx: RunContext, result: NLUResult) -> list[str]:
    """Merge the model's proposed patch. Material changes invalidate any pending approval."""
    st = ctx.state
    merged = merge_patch(st.request, st.sources, result.patch)
    st.request, st.sources = merged.request, merged.sources
    material = [f for f in merged.changed if f in MATERIAL_FIELDS]
    if material and st.pending_approval is not None:
        invalidate_pending(ctx, f"request changed: {', '.join(material)}")
        ctx.say(r.approval_invalidated(f"you changed {', '.join(material)}"))
    if merged.changed:
        ctx.tracer.emit("request.updated", changed=merged.changed, sources=dict(st.sources))
    return merged.changed


def handle_request(ctx: RunContext, result: NLUResult) -> None:
    st = ctx.state
    if st.kind is None:
        st.kind = "booking"
    apply_patch(ctx, result)
    req = st.request
    if req.service is not None and req.service not in SERVICES:
        ctx.say(r.unsupported_service(req.service))
        st.request.service = None
        return
    memory_cmd.apply_defaults(ctx)
    if req.same_provider_as_last and req.preferred_provider_id is None and req.service:
        _resolve_last_provider(ctx, req.service)
    missing = req.missing()
    if missing:
        if st.status != State.GATHERING:
            ctx.move(State.GATHERING, "user_message", f"missing {missing}")
        st.asked_fields = missing
        ctx.say(r.ask_missing(missing, req.service))
        ctx.save()
        return
    search_and_present(ctx)


def _resolve_last_provider(ctx: RunContext, service: str) -> None:
    out = ctx.tool("list_bookings", c.ListBookingsInput(user_id=ctx.user_id), c.ListBookingsOutput)
    past = [b for b in out.bookings if b.service == service and b.status == "completed"]
    if past:
        last = max(past, key=lambda b: b.slot)
        ctx.state.request.preferred_provider_id = last.provider_id
        ctx.tracer.emit("request.resolved_last_provider", provider_id=last.provider_id)


def search_and_present(ctx: RunContext) -> None:
    st = ctx.state
    req = st.request
    assert req.service is not None
    assert req.area is not None
    assert req.date_start is not None
    ctx.move(State.SEARCHING, "user_message", "all required details present")
    rng = c.DateRange(start=req.date_start, end=req.date_end or req.date_start)
    found = ctx.tool(
        "search_providers",
        c.SearchProvidersInput(service=req.service, area=req.area, date_range=rng),
        c.SearchProvidersOutput,
    )
    ranked = rank(found.providers, req, now=ctx.clock.now())
    if not ranked.candidates:
        ctx.move(State.GATHERING, "tool_result", "nothing fits")
        ctx.tracer.emit("search.nothing_fits", excluded=dict(ranked.excluded))
        ctx.say(r.nothing_fits(ranked.reason, ranked.suggestion))
        return
    st.candidates = [_inspect(ctx, cand) for cand in ranked.candidates]
    st.selected = None
    ctx.move(State.PRESENTING_OPTIONS, "tool_result", f"{len(st.candidates)} options")
    present(ctx)


def _inspect(ctx: RunContext, cand: Candidate) -> Candidate:
    """Fetch details and treat every free-text field as untrusted."""
    details = ctx.tool(
        "get_provider", c.GetProviderInput(provider_id=cand.provider_id), c.GetProviderOutput
    )
    name = sanitize(details.provider.name, ctx.settings.untrusted_max_chars)
    flags: dict[str, list[str]] = {}
    if name.flags:
        flags["provider_name"] = name.flags
    cleaned = []
    for i, review in enumerate(details.reviews):
        s = sanitize(review.text, ctx.settings.untrusted_max_chars)
        cleaned.append((review.rating, s))
        if s.flags:
            flags[f"review[{i}]"] = s.flags
    for channel, found in flags.items():
        ctx.tracer.emit(
            "security.injection_detected",
            provider_id=cand.provider_id,
            channel=channel,
            flags=list(found),
            action="flagged_and_ignored",
        )
    summary = summarize_reviews(
        llm=ctx.llm, settings=ctx.settings, tracer=ctx.tracer, budget=ctx.budget,
        provider_id=cand.provider_id, reviews=cleaned,
    )  # fmt: skip
    display_name = _safe_name(name.text) if name.flags else name.text
    flagged = bool(flags) or summary.injection_suspected
    return cand.model_copy(
        update={"name": display_name, "injection_flagged": flagged, "review_summary": summary}
    )


def _safe_name(text: str) -> str:
    """Drop anything after a bracket in a flagged provider name (that's where payloads hide)."""
    for sep in ("(", "[", "{", "<"):
        text = text.split(sep, 1)[0]
    return text.strip() or "Unnamed provider"


def present(ctx: RunContext) -> None:
    st = ctx.state
    lines = [r.options_intro(st.request.service, len(st.candidates))]
    lines.extend(r.option_line(i, cand) for i, cand in enumerate(st.candidates, 1))
    lines.append(r.options_outro())
    ctx.say("\n".join(lines))


def handle_selection(ctx: RunContext, sel: Selection | None) -> None:
    st = ctx.state
    choice = _resolve(st.candidates, sel)
    if choice is None:
        ctx.say(r.selection_unclear(len(st.candidates)) if st.candidates else r.didnt_understand())
        return
    cand, slot = choice
    if st.pending_approval is not None:
        invalidate_pending(ctx, "user selected a different option")
    if st.status == State.AWAITING_APPROVAL:
        ctx.move(State.PRESENTING_OPTIONS, "user_message", "re-selection")
    st.selected = cand.model_copy(update={"slot": slot})
    hold_selected(ctx, previous_price=None, previous_slot=None)


def _resolve(
    candidates: list[Candidate], sel: Selection | None
) -> tuple[Candidate, datetime] | None:
    if sel is None or not candidates:
        return None
    cand: Candidate | None = None
    if sel.option_number is not None and 1 <= sel.option_number <= len(candidates):
        cand = candidates[sel.option_number - 1]
    elif sel.provider_id is not None:
        cand = next((x for x in candidates if x.provider_id == sel.provider_id), None)
    elif len(candidates) == 1:
        cand = candidates[0]
    if cand is None:
        return None
    if sel.hour is None:
        return cand, cand.slot
    for slot in [cand.slot, *cand.other_slots]:
        if to_ist(slot).hour == sel.hour:
            return cand, slot
    return None


def hold_selected(
    ctx: RunContext, previous_price: int | None, previous_slot: datetime | None
) -> bool:
    """Hold the selected slot and ask for approval. Returns False if the slot is gone."""
    st = ctx.state
    cand = st.selected
    assert cand is not None
    if st.status != State.HOLDING:
        ctx.move(State.HOLDING, "user_message", f"hold {cand.provider_id}")
    try:
        held = ctx.tool(
            "hold_slot",
            c.HoldSlotInput(provider_id=cand.provider_id, slot=cand.slot),
            c.HoldSlotOutput,
        )
    except InvalidRequest:
        st.candidates = [x for x in st.candidates if x.slot != cand.slot]
        ctx.move(State.PRESENTING_OPTIONS, "tool_result", "slot unavailable")
        ctx.say(r.hold_expired_lost())
        if st.candidates:
            present(ctx)
        return False
    st.hold = HoldInfo(
        hold_id=held.hold_id,
        provider_id=held.provider_id,
        provider_name=cand.name,
        slot=held.slot,
        expires_at=held.expires_at,
        quoted_price_paise=held.quoted_price.paise,
        search_price_paise=cand.price_paise,
    )
    if previous_price is None and held.quoted_price.paise != cand.price_paise:
        previous_price = cand.price_paise
        ctx.tracer.emit(
            "price.changed",
            provider_id=cand.provider_id,
            search_price_paise=cand.price_paise,
            quoted_price_paise=held.quoted_price.paise,
        )
    ctx.move(State.AWAITING_APPROVAL, "tool_result", f"hold {held.hold_id}")
    request_approval(
        ctx,
        action="book",
        provider_id=held.provider_id,
        provider_name=cand.name,
        slot=held.slot,
        price_paise=held.quoted_price.paise,
        hold_id=held.hold_id,
        previous_price_paise=previous_price,
        previous_slot=previous_slot,
        hold_expires_at=held.expires_at,
    )
    return True
