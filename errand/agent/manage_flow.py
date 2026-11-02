"""Cancel and reschedule flows for existing bookings.

Ownership is checked in code against `list_bookings(current user)`: a booking ID that is not in
the current user's own list is refused, even when the ID is supplied verbatim. The current user
comes from session config, never from the model or tool output.
"""

from __future__ import annotations

from datetime import date, datetime

from errand.agent import responses as r
from errand.agent.approval_flow import invalidate_pending, request_approval
from errand.agent.nlu import BookingRef, RescheduleTo, Selection
from errand.agent.runtime import RunContext
from errand.agent.state import State, TargetBooking
from errand.domain.time import to_ist
from errand.tools import contracts as c


def _upcoming(ctx: RunContext) -> list[c.BookingRecord]:
    out = ctx.tool("list_bookings", c.ListBookingsInput(user_id=ctx.user_id), c.ListBookingsOutput)
    return [b for b in out.bookings if b.status == "upcoming"]


def resolve_target(ctx: RunContext, ref: BookingRef | None) -> TargetBooking | None:
    upcoming = _upcoming(ctx)
    if ref is not None and ref.booking_id:
        match = next((b for b in upcoming if b.booking_id == ref.booking_id), None)
        if match is None:
            ctx.tracer.emit(
                "security.ownership_refused", booking_id=ref.booking_id, user_id=ctx.user_id
            )
            ctx.say(r.not_your_booking())
            return None
        return _target(match)
    service = ref.service if ref else None
    matches = [b for b in upcoming if service is None or b.service == service]
    if not matches:
        ctx.say(r.no_matching_booking(r.service_name(service) if service else ""))
        return None
    if len(matches) > 1:
        ctx.say(
            r.which_booking([f"- {b.booking_id}: {b.provider}, {r.when(b.slot)}" for b in matches])
        )
        return None
    return _target(matches[0])


def _target(b: c.BookingRecord) -> TargetBooking:
    return TargetBooking(
        booking_id=b.booking_id,
        provider_id=b.provider_id,
        provider_name=b.provider,
        service=b.service,
        slot=b.slot,
        price_paise=b.price.paise,
    )


def _begin(ctx: RunContext, kind: str) -> None:
    st = ctx.state
    if st.pending_approval is not None:
        invalidate_pending(ctx, f"user started a {kind}")
    if st.status in (State.COMPLETED, State.FAILED_SAFE, State.ABANDONED):
        ctx.move(State.IDLE, "user_message", "new workflow")
        st.reset_workflow()


def start_cancel(ctx: RunContext, ref: BookingRef | None) -> None:
    _begin(ctx, "cancellation")
    target = resolve_target(ctx, ref)
    if target is None:
        return
    st = ctx.state
    st.kind = "cancel"
    st.target_booking = target
    ctx.move(State.CANCEL_FLOW, "user_message", f"cancel {target.booking_id}")
    ctx.move(State.AWAITING_APPROVAL, "user_message", "cancellation needs approval")
    request_approval(
        ctx,
        action="cancel",
        provider_id=target.provider_id,
        provider_name=target.provider_name,
        slot=target.slot,
        price_paise=target.price_paise,
        booking_id=target.booking_id,
    )


def start_reschedule(ctx: RunContext, ref: BookingRef | None, to: RescheduleTo | None) -> None:
    _begin(ctx, "reschedule")
    target = resolve_target(ctx, ref)
    if target is None:
        return
    st = ctx.state
    st.kind = "reschedule"
    st.target_booking = target
    ctx.move(State.RESCHEDULE_FLOW, "user_message", f"reschedule {target.booking_id}")
    if to is None or to.date is None:
        ctx.say(f"When would you like to move your {r.service_name(target.service)} to?")
        return
    propose_new_slot(ctx, to.date, to.hour)


def propose_new_slot(ctx: RunContext, day: date, hour: int | None) -> None:
    st = ctx.state
    target = st.target_booking
    assert target is not None
    slots = _slots_on(ctx, target, day)
    wanted = hour if hour is not None else to_ist(target.slot).hour
    earliest = st.request.earliest_hour
    chosen = next((s for s in slots if to_ist(s).hour == wanted), None)
    if chosen is None:
        fitting = [s for s in slots if earliest is None or to_ist(s).hour >= earliest]
        if hour is None and fitting:
            chosen = fitting[0]
    if chosen is None:
        st.alternatives = slots[:3]
        ctx.say(r.slot_conflict(_at(day, wanted, target.slot), st.alternatives))
        return
    ask_reschedule_approval(ctx, chosen)


def _at(day: date, hour: int, like: datetime) -> datetime:
    local = to_ist(like)
    return local.replace(year=day.year, month=day.month, day=day.day, hour=hour)


def _slots_on(ctx: RunContext, target: TargetBooking, day: date) -> list[datetime]:
    out = ctx.tool(
        "search_providers",
        c.SearchProvidersInput(
            service=target.service,
            area=ctx.state.request.area or "unspecified",
            date_range=c.DateRange(start=day, end=day),
        ),
        c.SearchProvidersOutput,
    )
    provider = next((p for p in out.providers if p.provider_id == target.provider_id), None)
    return list(provider.available_slots) if provider else []


def ask_reschedule_approval(ctx: RunContext, slot: datetime) -> None:
    st = ctx.state
    target = st.target_booking
    assert target is not None
    st.new_slot = slot
    if st.status != State.RESCHEDULE_FLOW:
        ctx.move(State.RESCHEDULE_FLOW, "user_message", "new slot chosen")
    ctx.move(State.AWAITING_APPROVAL, "user_message", "reschedule needs approval")
    request_approval(
        ctx,
        action="reschedule",
        provider_id=target.provider_id,
        provider_name=target.provider_name,
        slot=slot,
        price_paise=target.price_paise,
        booking_id=target.booking_id,
        previous_slot=target.slot,
    )


def handle_alternative(ctx: RunContext, sel: Selection | None) -> None:
    alts = ctx.state.alternatives
    n = sel.option_number if sel else None
    if n is None or not 1 <= n <= len(alts):
        ctx.say(r.selection_unclear(len(alts)) if alts else r.didnt_understand())
        return
    ask_reschedule_approval(ctx, alts[n - 1])
