"""Creating, presenting, invalidating and granting approval requests.

Only this module creates ApprovalRequests. Whether a user message approves one is decided by
`classify_reply` (deterministic rules over the raw latest user message); the grant itself is
minted by booking.execute in the same transaction that consumes it and opens the intent.
"""

from __future__ import annotations

from datetime import datetime

from errand.agent import responses as r
from errand.agent.approval import Action, ApprovalRequest, Decision, classify_confirmation
from errand.agent.runtime import RunContext


def request_approval(
    ctx: RunContext,
    *,
    action: Action,
    provider_id: str,
    provider_name: str,
    slot: datetime,
    price_paise: int,
    hold_id: str | None = None,
    booking_id: str | None = None,
    previous_price_paise: int | None = None,
    previous_slot: datetime | None = None,
    hold_expires_at: datetime | None = None,
) -> ApprovalRequest:
    if ctx.state.pending_approval is not None:
        invalidate_pending(ctx, "superseded by a new request")
    req = ApprovalRequest(
        request_id=ctx.ids.new("ar"),
        action=action,
        user_id=ctx.user_id,
        provider_id=provider_id,
        provider_name=provider_name,
        slot=slot,
        price_paise=price_paise,
        hold_id=hold_id,
        booking_id=booking_id,
        nonce=ctx.ids.new("n"),
        created_at=ctx.clock.now(),
        previous_price_paise=previous_price_paise,
        previous_slot=previous_slot,
    )
    ctx.store.save_approval(req, ctx.state.session_id)
    ctx.state.pending_approval = req
    ctx.tracer.emit(
        "approval.requested",
        request_id=req.request_id,
        action=action,
        fingerprint=req.fingerprint,
        confirm_code=req.confirm_code,
        provider_id=provider_id,
        slot=slot.isoformat(),
        price_paise=price_paise,
        hold_id=hold_id,
        booking_id=booking_id,
    )
    ctx.say(r.approval_card(req, hold_expires_at))
    return req


def invalidate_pending(ctx: RunContext, reason: str) -> None:
    req = ctx.state.pending_approval
    if req is None:
        return
    ctx.store.set_approval_status(req.request_id, "invalidated", reason)
    ctx.state.pending_approval = None
    ctx.tracer.emit(
        "approval.invalidated",
        request_id=req.request_id,
        fingerprint=req.fingerprint,
        reason=reason,
    )


def reissue(ctx: RunContext, reason: str) -> ApprovalRequest | None:
    """Invalidate the pending request and issue an identical one with a fresh nonce/code."""
    old = ctx.state.pending_approval
    if old is None:
        return None
    invalidate_pending(ctx, reason)
    hold = ctx.state.hold
    return request_approval(
        ctx,
        action=old.action,
        provider_id=old.provider_id,
        provider_name=old.provider_name,
        slot=old.slot,
        price_paise=old.price_paise,
        hold_id=old.hold_id,
        booking_id=old.booking_id,
        hold_expires_at=hold.expires_at if hold and hold.hold_id == old.hold_id else None,
    )


def classify_reply(ctx: RunContext, user_text: str) -> Decision:
    """Apply the deterministic confirmation rules to the raw latest user message."""
    req = ctx.state.pending_approval
    if req is None:
        return Decision.NOT_APPROVAL
    decision = classify_confirmation(user_text, req.confirm_code)
    ctx.tracer.emit("approval.classified", request_id=req.request_id, decision=decision.value)
    return decision
