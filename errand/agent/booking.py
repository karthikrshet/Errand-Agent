"""Idempotent execution of approved side effects, and recovery.

Order of operations for `book` (the same shape applies to cancel/reschedule):
1. Re-derive the ApprovalRequest fingerprint from the *exact call we are about to make* and
   require it to equal the grant's fingerprint (so a changed hold/slot/price can never ride on an
   old approval).
2. In ONE transaction: consume the single-use grant and write a PENDING intent whose
   idempotency key is derived from (user_id, fingerprint).
3. Call the tool with that key. On timeout or any ambiguous failure, ask
   `get_booking_status` *first*; only retry (same key, bounded) if it says not booked and the
   hold is still valid.
4. Only a direct success or a verified status lets us tell the user "booked".
"""

from __future__ import annotations

from errand.agent import responses as r
from errand.agent.approval import ApprovalRequest, idempotency_key_for
from errand.agent.approval_flow import request_approval
from errand.agent.runtime import RunContext
from errand.agent.state import State
from errand.agent.store import BookingIntent
from errand.errors import ApprovalInvalid, ErrandError, ToolTimeout
from errand.tools import contracts as c

MAX_BOOK_ATTEMPTS = 3


def execute(ctx: RunContext) -> None:
    """Called only after the user's latest message was classified AFFIRM for the pending
    request. Mints the single-use grant, consumes it and opens the intent atomically."""
    req = ctx.state.pending_approval
    if req is None:
        raise ApprovalInvalid("no pending approval")
    _check_request(ctx, req)
    if req.action in ("cancel", "reschedule") and not _target_still_active(ctx, req):
        return
    opened = ctx.store.grant_and_open_intent(
        req,
        session_id=ctx.state.session_id,
        source_turn_id=ctx.user_turn_id or 0,
        now=ctx.clock.now(),
        grant_id=ctx.ids.new("g"),
        intent_id=ctx.ids.new("i"),
    )
    if opened is None:
        _duplicate_confirmation(ctx, req)
        return
    grant, intent = opened
    ctx.tracer.emit(
        "approval.granted",
        session_id=ctx.state.session_id,
        request_id=req.request_id,
        grant_id=grant.grant_id,
        fingerprint=grant.fingerprint,
        source=grant.source,
        source_turn_id=ctx.user_turn_id,
    )
    ctx.tracer.emit(
        "approval.consumed",
        grant_id=grant.grant_id,
        intent_id=intent.intent_id,
        idempotency_key=intent.idempotency_key,
    )
    ctx.state.pending_approval = None
    ctx.state.intent_id = intent.intent_id
    ctx.move(State.EXECUTING, "approval_grant", f"grant {grant.grant_id}")
    ctx.crash.hit("after_intent_write")
    if req.action == "book":
        _book(ctx, intent)
    elif req.action == "cancel":
        _cancel(ctx, intent)
    else:
        _reschedule(ctx, intent)


def _target_still_active(ctx: RunContext, req: ApprovalRequest) -> bool:
    """Re-read the booking right before granting: the provider may have cancelled it since
    the card was shown, and 'Cancelled and verified' must not describe someone else's act."""
    out = ctx.tool("list_bookings", c.ListBookingsInput(user_id=ctx.user_id), c.ListBookingsOutput)
    current = next((b for b in out.bookings if b.booking_id == req.booking_id), None)
    if current is not None and current.status == "upcoming":
        return True
    status = current.status if current is not None else "not found"
    ctx.tracer.emit("approval.rejected_at_execution", problems=[f"target booking {status}"])
    ctx.state.pending_approval = None
    ctx.invalidate(req, f"target booking {status}")
    ctx.move(State.ABANDONED, "user_message", f"target booking {status}")
    ctx.say(r.target_gone(req.booking_id or "", status.replace("_", " ")))
    return False


def _check_request(ctx: RunContext, req: ApprovalRequest) -> None:
    """The approval must match the exact call we would make: same user, same live hold."""
    problems = []
    if ctx.state.status != State.AWAITING_APPROVAL:
        problems.append(f"session is {ctx.state.status.value}, not awaiting approval")
    if req.user_id != ctx.user_id:
        problems.append("user mismatch")
    if req.action == "book":
        hold = ctx.state.hold
        if hold is None or hold.hold_id != req.hold_id:
            problems.append("hold changed")
        elif hold.slot != req.slot or hold.provider_id != req.provider_id:
            problems.append("slot or provider changed")
        elif hold.quoted_price_paise != req.price_paise:
            problems.append("price changed")
    if problems:
        ctx.tracer.emit("approval.rejected_at_execution", problems=problems)
        raise ApprovalInvalid("; ".join(problems))


def _duplicate_confirmation(ctx: RunContext, req: ApprovalRequest) -> None:
    """The approval is no longer pending: report what happened instead of acting twice."""
    existing = ctx.store.intent_by_key(idempotency_key_for(ctx.user_id, req.fingerprint))
    if existing is None:
        ctx.state.pending_approval = None
        ctx.say(r.approval_invalidated("it was replaced or withdrawn"))
        return
    ctx.tracer.emit(
        "approval.duplicate_confirmation",
        intent_id=existing.intent_id if existing else None,
    )
    if existing is not None:
        ctx.state.intent_id = existing.intent_id
        _finish_from_intent(ctx, existing)


# ----------------------------------------------------------------------------- book


def _book(ctx: RunContext, intent: BookingIntent) -> None:
    req = intent.request
    assert req.hold_id is not None
    for attempt in range(1, MAX_BOOK_ATTEMPTS + 1):
        try:
            out = ctx.tool(
                "book",
                c.BookInput(hold_id=req.hold_id, idempotency_key=intent.idempotency_key),
                c.BookOutput,
            )
        except ToolTimeout:
            if ctx.state.status != State.VERIFYING:
                ctx.move(State.VERIFYING, "tool_result", "book timed out")
            status = _status(ctx, intent)
            if status is None:
                return
            if status.status == "booked":
                assert status.booking_id is not None
                _succeeded(ctx, intent, status.booking_id)
                return
            if status.hold_expired:
                _hold_expired(ctx, intent)
                return
            ctx.tracer.emit("booking.retry_same_key", attempt=attempt + 1)
            ctx.move(State.EXECUTING, "approval_grant", "retry with the same key")
            continue
        ctx.crash.hit("after_book_call_before_response")
        _handle_book_output(ctx, intent, out)
        return
    _unverified(ctx, intent, "book attempts exhausted")


def _handle_book_output(ctx: RunContext, intent: BookingIntent, out: c.BookOutput) -> None:
    if out.outcome == "booked":
        assert out.booking_id is not None
        _succeeded(ctx, intent, out.booking_id)
    elif out.outcome == "slot_taken":
        _slot_taken(ctx, intent)
    else:
        _hold_expired(ctx, intent)


def _status(ctx: RunContext, intent: BookingIntent) -> c.BookingStatusOutput | None:
    """Ask the source of truth. If even that fails, leave the intent PENDING for reconcile."""
    try:
        return ctx.tool(
            "get_booking_status",
            c.GetBookingStatusInput(idempotency_key=intent.idempotency_key),
            c.BookingStatusOutput,
        )
    except ErrandError as exc:
        _unverified(ctx, intent, f"status check failed: {type(exc).__name__}")
        return None


def _succeeded(ctx: RunContext, intent: BookingIntent, booking_id: str) -> None:
    req = intent.request
    ctx.store.set_intent(intent.intent_id, "SUCCEEDED", {"booking_id": booking_id}, ctx.clock.now())
    ctx.tracer.emit(
        "booking.verified",
        intent_id=intent.intent_id,
        booking_id=booking_id,
        action=req.action,
        idempotency_key=intent.idempotency_key,
    )
    ctx.crash.hit("after_booking_before_state_save")
    ctx.state.hold = None
    ctx.move(State.COMPLETED, "tool_result", f"verified {booking_id}")
    if req.action == "book":
        ctx.say(r.booked(booking_id, req.provider_name, req.slot, req.price_paise))
        _maybe_infer(ctx)
    elif req.action == "cancel":
        ctx.say(r.cancelled(booking_id, req.provider_name, req.slot))
    else:
        ctx.say(r.rescheduled(booking_id, req.provider_name, req.slot))


def _slot_taken(ctx: RunContext, intent: BookingIntent) -> None:
    from errand.agent.search_flow import present  # noqa: PLC0415 - avoid import cycle

    ctx.store.set_intent(intent.intent_id, "FAILED", {"reason": "slot_taken"}, ctx.clock.now())
    st = ctx.state
    taken = intent.request
    st.hold = None
    st.candidates = [
        cand.model_copy(update={"slot": cand.other_slots[0], "other_slots": cand.other_slots[1:]})
        if cand.provider_id == taken.provider_id and cand.slot == taken.slot and cand.other_slots
        else cand
        for cand in st.candidates
        if not (cand.provider_id == taken.provider_id and cand.slot == taken.slot)
        or cand.other_slots
    ]
    ctx.move(State.PRESENTING_OPTIONS, "tool_result", "slot_taken")
    ctx.say(r.slot_taken("Here are the remaining options:" if st.candidates else ""))
    if st.candidates:
        present(ctx)


def _hold_expired(ctx: RunContext, intent: BookingIntent) -> None:
    """Re-hold the same slot and ask again: a new hold is a new approval, always."""
    from errand.agent.search_flow import hold_selected  # noqa: PLC0415 - avoid import cycle

    ctx.store.set_intent(intent.intent_id, "FAILED", {"reason": "hold_expired"}, ctx.clock.now())
    st = ctx.state
    if st.selected is None:
        _unverified(ctx, intent, "hold expired without a selection")
        return
    ctx.say(r.hold_expired_reheld())
    st.selected = st.selected.model_copy(update={"slot": intent.request.slot})
    ctx.move(State.HOLDING, "tool_result", "hold expired; re-holding")
    hold_selected(ctx, previous_price=intent.request.price_paise, previous_slot=intent.request.slot)


def _unverified(ctx: RunContext, intent: BookingIntent, why: str) -> None:
    """We do not know. Say so, keep the intent PENDING, and reconcile before anything else."""
    ctx.tracer.emit("booking.unverified", intent_id=intent.intent_id, reason=why)
    if ctx.state.status != State.FAILED_SAFE:
        ctx.move(State.FAILED_SAFE, "system", why)
    ctx.say(r.booking_unverified())


# ----------------------------------------------------------------------------- cancel / reschedule


def _cancel(ctx: RunContext, intent: BookingIntent) -> None:
    req = intent.request
    assert req.booking_id is not None
    out = ctx.tool(
        "cancel",
        c.CancelInput(booking_id=req.booking_id, idempotency_key=intent.idempotency_key),
        c.CancelOutput,
    )
    _succeeded(ctx, intent, out.booking_id)


def _reschedule(ctx: RunContext, intent: BookingIntent) -> None:
    req = intent.request
    assert req.booking_id is not None
    out = ctx.tool(
        "reschedule",
        c.RescheduleInput(
            booking_id=req.booking_id, slot=req.slot, idempotency_key=intent.idempotency_key
        ),
        c.RescheduleOutput,
    )
    if out.outcome == "rescheduled":
        _succeeded(ctx, intent, out.booking_id)
        return
    ctx.store.set_intent(intent.intent_id, "FAILED", {"reason": "slot_conflict"}, ctx.clock.now())
    ctx.state.alternatives = list(out.alternative_slots)
    ctx.move(State.RESCHEDULE_FLOW, "approval_grant", "slot_conflict")
    ctx.say(r.slot_conflict(req.slot, out.alternative_slots))


# ----------------------------------------------------------------------------- recovery


def _finish_from_intent(ctx: RunContext, intent: BookingIntent) -> None:
    """Duplicate confirmation: report what the first confirmation already achieved."""
    if intent.status == "PENDING":
        reconcile_intent(ctx, intent)
        return
    if intent.status == "SUCCEEDED" and intent.result:
        req = intent.request
        ctx.say(r.booked(intent.result.get("booking_id", ""), req.provider_name, req.slot,
                         req.price_paise))  # fmt: skip
    else:
        ctx.say(r.reconcile_not_booked())
    settle_session(ctx, intent)


def recover(ctx: RunContext) -> None:
    """Run before any new input: resolve PENDING intents, then un-stick this session."""
    for intent in ctx.store.pending_intents(ctx.user_id):
        ctx.tracer.emit("reconcile.start", intent_id=intent.intent_id, action=intent.action)
        reconcile_intent(ctx, intent)
    st = ctx.state
    if st.status in (State.EXECUTING, State.VERIFYING) and st.intent_id:
        current = ctx.store.intent(st.intent_id)
        if current is not None:
            if current.status == "SUCCEEDED" and not _reverify(ctx, current):
                return
            settle_session(ctx, current)


def _reverify(ctx: RunContext, intent: BookingIntent) -> bool:
    """Our own record says SUCCEEDED, but this run has not verified it: ask the source of truth
    before telling the user anything (see docs/BUGS.md #3)."""
    if intent.action != "book":
        booking_id = (intent.result or {}).get("booking_id", "")
        ctx.say(r.reconcile_booked(booking_id))
        ctx.tracer.emit("booking.verified", intent_id=intent.intent_id, booking_id=booking_id,
                        action=intent.action, via="intent_record")  # fmt: skip
        return True
    status = ctx.gateway.call(
        "get_booking_status",
        c.GetBookingStatusInput(idempotency_key=intent.idempotency_key),
        c.BookingStatusOutput,
        state=State.VERIFYING,
    )
    if status.status != "booked" or status.booking_id is None:
        ctx.tracer.emit("booking.record_mismatch", intent_id=intent.intent_id, status=status.status)
        _unverified(ctx, intent, "intent record disagrees with provider")
        return False
    ctx.tracer.emit(
        "booking.verified",
        intent_id=intent.intent_id,
        booking_id=status.booking_id,
        action="book",
        idempotency_key=intent.idempotency_key,
        via="restart_check",
    )
    ctx.say(r.reconcile_booked(status.booking_id))
    return True


def reconcile_intent(ctx: RunContext, intent: BookingIntent) -> None:
    """Ask the source of truth about one PENDING intent and record the answer."""
    if intent.action != "book":
        _resend_side_effect(ctx, intent)
        return
    try:
        status = ctx.gateway.call(
            "get_booking_status",
            c.GetBookingStatusInput(idempotency_key=intent.idempotency_key),
            c.BookingStatusOutput,
            state=State.VERIFYING,
        )
    except ErrandError as exc:
        ctx.tracer.emit("reconcile.deferred", intent_id=intent.intent_id, error=str(exc))
        return
    if status.status == "booked":
        assert status.booking_id is not None
        ctx.store.set_intent(
            intent.intent_id, "SUCCEEDED", {"booking_id": status.booking_id}, ctx.clock.now()
        )
        ctx.tracer.emit(
            "booking.verified",
            intent_id=intent.intent_id,
            booking_id=status.booking_id,
            action="book",
            idempotency_key=intent.idempotency_key,
            via="reconcile",
        )
        ctx.say(r.reconcile_booked(status.booking_id))
    else:
        ctx.store.set_intent(
            intent.intent_id, "FAILED", {"reason": f"reconciled:{status.status}"}, ctx.clock.now()
        )
        ctx.tracer.emit("reconcile.not_booked", intent_id=intent.intent_id, status=status.status)
        ctx.say(r.reconcile_not_booked())
    if ctx.state.intent_id == intent.intent_id:
        refreshed = ctx.store.intent(intent.intent_id)
        if refreshed is not None:
            settle_session(ctx, refreshed)


def _resend_side_effect(ctx: RunContext, intent: BookingIntent) -> None:
    """Cancel/reschedule are idempotent per key and were approved: re-sending is safe."""
    req = intent.request
    assert req.booking_id is not None
    try:
        if intent.action == "cancel":
            ctx.gateway.call(
                "cancel",
                c.CancelInput(booking_id=req.booking_id, idempotency_key=intent.idempotency_key),
                c.CancelOutput,
                state=State.EXECUTING,
            )
            ok = True
        else:
            out = ctx.gateway.call(
                "reschedule",
                c.RescheduleInput(
                    booking_id=req.booking_id, slot=req.slot,
                    idempotency_key=intent.idempotency_key,
                ),
                c.RescheduleOutput,
                state=State.EXECUTING,
            )  # fmt: skip
            ok = out.outcome == "rescheduled"
    except ErrandError as exc:
        ctx.tracer.emit("reconcile.deferred", intent_id=intent.intent_id, error=str(exc))
        return
    result = {"booking_id": req.booking_id} if ok else {"reason": "slot_conflict"}
    ctx.store.set_intent(intent.intent_id, "SUCCEEDED" if ok else "FAILED", result,
                         ctx.clock.now())  # fmt: skip
    ctx.say(r.reconcile_booked(req.booking_id) if ok else r.reconcile_not_booked())
    if ctx.state.intent_id == intent.intent_id:
        refreshed = ctx.store.intent(intent.intent_id)
        if refreshed is not None:
            settle_session(ctx, refreshed)


def settle_session(ctx: RunContext, intent: BookingIntent) -> None:
    """Move a session that was mid-execution to a resting state that matches the intent."""
    st = ctx.state
    if st.status not in (State.EXECUTING, State.VERIFYING):
        return
    if intent.status == "SUCCEEDED":
        st.hold = None
        ctx.move(State.COMPLETED, "tool_result", "reconciled: done")
        return
    hold = st.hold
    alive = hold is not None and hold.hold_id == intent.request.hold_id
    if intent.action == "book" and hold is not None and alive and hold.expires_at > ctx.clock.now():
        # Never auto-retry after a restart: the user re-confirms against a fresh request.
        ctx.move(State.AWAITING_APPROVAL, "system", "re-offer after interrupted booking")
        request_approval(
            ctx,
            action="book",
            provider_id=hold.provider_id,
            provider_name=hold.provider_name,
            slot=hold.slot,
            price_paise=hold.quoted_price_paise,
            hold_id=hold.hold_id,
            hold_expires_at=hold.expires_at,
        )
        return
    ctx.move(State.FAILED_SAFE, "system", "reconciled: not done")


def _maybe_infer(ctx: RunContext) -> None:
    """Best effort: preference inference must never affect a verified booking's outcome."""
    from errand.memory.commands import infer_preferences  # noqa: PLC0415 - avoid import cycle

    try:
        infer_preferences(ctx)
    except ErrandError as exc:
        ctx.tracer.emit("memory.infer_skipped", error=type(exc).__name__)
