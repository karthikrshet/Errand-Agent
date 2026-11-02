"""Proactive follow-up when a provider cancels a booking.

Runs without any user message (trigger `proactive_event`). It polls `list_bookings`, which is
the same source of truth the user would see, rather than trusting a side channel. For each newly
detected provider cancellation it:

1. decides whether to notify (upcoming: yes; past: no) and logs the decision and reason;
2. searches replacements within the user's *active* preferences (area, earliest time, budget,
   blocked providers) and never re-offers the provider who just cancelled;
3. holds the best option and asks for approval with a brand-new ApprovalRequest. The original
   booking's approval was consumed long ago and is never reused;
4. if nothing fits, explains why and offers to relax one constraint.
"""

from __future__ import annotations

from datetime import timedelta

from errand.agent import responses as r
from errand.agent.ranking import rank
from errand.agent.runtime import RunContext
from errand.agent.search_flow import hold_selected, inspect_candidate
from errand.agent.state import TERMINAL, State
from errand.domain.request import ServiceRequest
from errand.domain.time import to_ist
from errand.memory.commands import apply_defaults
from errand.tools import contracts as c

REPLACEMENT_WINDOW_DAYS = 3
RESTING = frozenset({State.IDLE, *TERMINAL})


def check(ctx: RunContext) -> None:
    out = ctx.tool("list_bookings", c.ListBookingsInput(user_id=ctx.user_id), c.ListBookingsOutput)
    seen = ctx.store.proactive_seen(ctx.user_id)
    for b in out.bookings:
        if b.status != "cancelled_by_provider" or b.booking_id in seen:
            continue
        if b.slot <= ctx.clock.now():
            ctx.store.mark_proactive_seen(ctx.user_id, b.booking_id, ctx.clock.now())
            ctx.tracer.emit("proactive.detected", booking_id=b.booking_id)
            _decide(ctx, b, notify=False, reason="booking was in the past; nothing to replace")
            continue
        if ctx.state.status not in RESTING:
            # Defer, don't drop: tell the user once, keep it pending, replace when they're done.
            if ctx.store.mark_proactive_notified(ctx.user_id, b.booking_id, ctx.clock.now()):
                ctx.tracer.emit("proactive.detected", booking_id=b.booking_id)
                _decide(ctx, b, notify=True, reason="user is mid-task; replacement deferred")
                ctx.say(r.provider_cancelled(b.provider, b.slot) + " " + r.proactive_later())
            continue
        ctx.store.mark_proactive_seen(ctx.user_id, b.booking_id, ctx.clock.now())
        ctx.tracer.emit("proactive.detected", booking_id=b.booking_id, provider_id=b.provider_id)
        _decide(ctx, b, notify=True, reason="upcoming booking; looking for a replacement")
        _replace(ctx, b)
        return  # one replacement flow at a time


def _decide(ctx: RunContext, b: c.BookingRecord, *, notify: bool, reason: str) -> None:
    ctx.tracer.emit("proactive.decision", booking_id=b.booking_id, notify=notify, reason=reason)


def _replace(ctx: RunContext, b: c.BookingRecord) -> None:
    st = ctx.state
    if st.status != State.IDLE:
        ctx.move(State.IDLE, "proactive_event", "provider cancellation")
    st.reset_workflow()
    day = to_ist(b.slot).date()
    st.kind = "proactive"
    st.replaces_booking_id = b.booking_id
    st.request = ServiceRequest(
        service=b.service,
        date_start=day,
        date_end=day + timedelta(days=REPLACEMENT_WINDOW_DAYS),
        blocked_provider_ids=[b.provider_id],
    )
    apply_defaults(ctx)
    req = st.request
    req.area = req.area or "unspecified"
    ctx.move(State.PROACTIVE_REPLACEMENT, "proactive_event", f"replace {b.booking_id}")
    ctx.say(r.provider_cancelled(b.provider, b.slot))
    found = ctx.tool(
        "search_providers",
        c.SearchProvidersInput(
            service=b.service,
            area=req.area,
            date_range=c.DateRange(start=day, end=day + timedelta(days=REPLACEMENT_WINDOW_DAYS)),
        ),
        c.SearchProvidersOutput,
    )
    ranked = rank(found.providers, req, now=ctx.clock.now())
    if not ranked.candidates:
        ctx.tracer.emit("proactive.nothing_fits", excluded=ranked.excluded)
        ctx.move(State.ABANDONED, "proactive_event", "no replacement within preferences")
        ctx.say(r.nothing_fits(ranked.reason, ranked.suggestion))
        return
    best = inspect_candidate(ctx, ranked.candidates[0])
    st.candidates = [best, *ranked.candidates[1:]]
    st.selected = best
    ctx.say(r.proactive_offer(best.name, best.reasons, best.injection_flagged))
    hold_selected(ctx, previous_price=None, previous_slot=None, trigger="proactive_event")
