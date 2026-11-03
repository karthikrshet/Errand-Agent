"""Every user-facing message. Templates, not LLM prose, so that what the user approves is exactly
what the code will do: the approval card is rendered field-by-field from the ApprovalRequest.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from errand.agent.approval import ApprovalRequest
from errand.agent.state import Candidate
from errand.domain.money import format_inr
from errand.domain.time import to_ist

FIELD_QUESTIONS = {
    "service": "what service you need (e.g. plumber, electrician, AC servicing)",
    "area": "which area you're in",
    "date_start": "when you'd like it (a day or a date range)",
}
SERVICE_NAMES = {
    "plumber": "plumber",
    "electrician": "electrician",
    "ac_service": "AC servicing",
    "cleaning": "home cleaning",
}


def when(dt: datetime) -> str:
    local = to_ist(dt)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a %d %b').replace(' 0', ' ')}, {hour}:{local:%M} {local:%p}"


def service_name(service: str | None) -> str:
    return SERVICE_NAMES.get(service or "", service or "service")


# ----------------------------------------------------------------------------- gathering


def ask_missing(fields: Sequence[str], service: str | None) -> str:
    asks = [FIELD_QUESTIONS[f] for f in fields if f in FIELD_QUESTIONS]
    name = service_name(service)
    article = "an" if name[:1].lower() in "aeiou" else "a"
    what = f"{article} {name}" if service else "the right provider"
    if len(asks) == 1:
        return f"To find {what}, could you tell me {asks[0]}?"
    joined = ", ".join(asks[:-1]) + f" and {asks[-1]}"
    return f"To find {what}, could you tell me {joined}?"


def unsupported_service(service: str) -> str:
    known = ", ".join(sorted(SERVICE_NAMES.values()))
    return f"Sorry, I can't arrange '{service}' yet. I can help with: {known}."


def nothing_fits(reason: str, suggestion: str) -> str:
    return f"I couldn't find anything that fits: {reason}. {suggestion}"


def options_intro(service: str | None, count: int) -> str:
    return f"Here are the best {count} {service_name(service)} options I found:"


def option_line(n: int, cand: Candidate) -> str:
    badge = "  [!] suspicious review content ignored" if cand.injection_flagged else ""
    why = "; ".join(cand.reasons)
    rs = cand.review_summary
    if rs is not None:
        notes = ", ".join(t.replace("_", " ") for t in rs.themes) or "no clear themes"
        why += f"; reviews: {rs.sentiment} ({notes})"
    return (
        f"{n}. {cand.name} - {when(cand.slot)} - {format_inr(cand.price_paise)} - "
        f"{cand.rating:.1f}/5{badge}\n   Why: {why}"
    )


def options_outro() -> str:
    return "Reply with an option number (e.g. 'book option 1'), or tell me what to change."


def selection_unclear(count: int) -> str:
    return f"Which option would you like? Reply with a number from 1 to {count}."


# ----------------------------------------------------------------------------- approvals

_VERB = {"book": "BOOK", "cancel": "CANCEL", "reschedule": "RESCHEDULE"}


def approval_card(req: ApprovalRequest, hold_expires_at: datetime | None = None) -> str:
    lines = []
    if req.previous_price_paise is not None and req.previous_price_paise != req.price_paise:
        lines.append(
            f"Price changed from {format_inr(req.previous_price_paise)} to "
            f"{format_inr(req.price_paise)}. Approve the new price?"
        )
    if req.previous_slot is not None and req.previous_slot != req.slot:
        lines.append(f"Time changed from {when(req.previous_slot)} to {when(req.slot)}.")
    detail = f"{req.provider_name} - {when(req.slot)} - {format_inr(req.price_paise)}"
    if req.action == "cancel":
        detail = f"booking {req.booking_id}: {req.provider_name} on {when(req.slot)}"
    elif req.action == "reschedule":
        detail = f"booking {req.booking_id} with {req.provider_name} to {when(req.slot)}"
    lines.append(f"Please confirm: {_VERB[req.action]} {detail}.")
    if hold_expires_at is not None:
        lines.append(f"The slot is held until {to_ist(hold_expires_at):%I:%M %p}.")
    lines.append(f"Reply 'yes' or 'CONFIRM {req.confirm_code}' to go ahead, or 'no' to stop.")
    return "\n".join(lines)


def ambiguous_confirmation() -> str:
    return (
        "I haven't done anything yet: your reply sounded like a change or a question, so I "
        "won't treat it as approval. If you want exactly what's below, confirm it with the new "
        "code; otherwise tell me what to change."
    )


def ungrounded_intent(intent: str) -> str:
    what = "cancel" if intent == "cancel_booking" else "move"
    return (
        f"I didn't change anything. I can only {what} a booking when you ask for it in your own "
        f'words, for example "{what} my AC servicing".'
    )


def code_required(req: ApprovalRequest) -> str:
    return (
        'Nothing is booked yet. I raised the card above on my own, so a plain "yes" might '
        f"have been meant for something else. To approve it, reply CONFIRM {req.confirm_code}."
    )


def code_mismatch(req: ApprovalRequest) -> str:
    return (
        "That confirmation code doesn't match the current request, so nothing was done.\n"
        + approval_card(req)
    )


def provider_cancelled(provider: str, slot: datetime) -> str:
    return f"Heads-up: {provider} cancelled your booking for {when(slot)}. You won't be charged."


def proactive_later() -> str:
    return "I'll look for a replacement as soon as we've finished what we're doing now."


def proactive_offer(name: str, reasons: Sequence[str], flagged: bool) -> str:
    badge = (
        " [!] Some of its reviews contained suspicious text, which I ignored." if flagged else ""
    )
    return (
        f"I found a replacement within your known preferences: {name} ({'; '.join(reasons)})."
        f"{badge} I've held the slot but booked nothing; it needs your fresh approval."
    )


def nothing_to_confirm(options: int) -> str:
    return (
        "There's nothing waiting for your confirmation right now, so nothing was booked. "
        f"Pick an option (1-{options}) and I'll show you exactly what I'd book."
    )


def target_gone(booking_id: str, status: str) -> str:
    return f"Booking {booking_id} is no longer active ({status}), so I didn't change anything."


def busy() -> str:
    return (
        "I'm still working on your previous message in this conversation. Nothing new was "
        "done; please send that again in a moment."
    )


def already_done(booking_id: str | None) -> str:
    ref = f" (booking ID {booking_id})" if booking_id else ""
    return f"That's already done{ref}; there's nothing more to confirm."


def handled_elsewhere(status: str) -> str:
    return (
        "This conversation was updated from another window while I was working, so I kept "
        f"that version. Current step: {status}."
    )


def rejected() -> str:
    return "OK, I haven't booked or changed anything. Tell me what you'd like to do instead."


def approval_invalidated(reason: str) -> str:
    return f"Your earlier approval no longer applies ({reason}), so nothing was done."


# ----------------------------------------------------------------------------- outcomes


# The only phrases that claim a side effect happened. Invariant I3 imports these, so a reworded
# success message can never slip past it.
BOOKED = "Booked and verified"
CANCELLED = "Cancelled and verified"
RESCHEDULED = "Rescheduled and verified"
RECONCILED = "went through and is verified"
SUCCESS_PHRASES = (BOOKED, CANCELLED, RESCHEDULED, RECONCILED)


def booked(booking_id: str, provider: str, slot: datetime, price_paise: int) -> str:
    return (
        f"{BOOKED}: {provider} on {when(slot)} for {format_inr(price_paise)}. "
        f"Booking ID {booking_id}."
    )


def booking_unverified() -> str:
    return (
        "I couldn't get a clear answer from the provider, so I can't verify the result yet. "
        "I have NOT confirmed anything. I'll check the provider's records again before doing "
        "anything else, so nothing happens twice."
    )


def slot_taken(alternatives_text: str) -> str:
    return (
        f"Sorry, that slot was just taken by someone else. Nothing was booked.\n{alternatives_text}"
    )


def hold_expired_reheld() -> str:
    return (
        "The 10-minute hold expired before booking, so I placed a new hold. Please confirm again."
    )


def hold_expired_lost() -> str:
    return "The hold expired and that slot is no longer available. Nothing was booked."


def cancelled(booking_id: str, provider: str, slot: datetime) -> str:
    return f"{CANCELLED}: booking {booking_id} with {provider} on {when(slot)}."


def rescheduled(booking_id: str, provider: str, slot: datetime) -> str:
    return f"{RESCHEDULED}: booking {booking_id} with {provider} is now {when(slot)}."


def slot_conflict(slot: datetime, alternatives: Sequence[datetime]) -> str:
    if not alternatives:
        return f"{when(slot)} isn't available and I found no nearby alternatives."
    alts = "\n".join(f"{i}. {when(a)}" for i, a in enumerate(alternatives, 1))
    return (
        f"{when(slot)} isn't available for rescheduling. Nearby options:\n{alts}\n"
        "Reply with an option number."
    )


def no_matching_booking(what: str) -> str:
    return f"I couldn't find an upcoming {what} booking on your account."


def not_your_booking() -> str:
    return "I can't find that booking on your account, so I can't change it."


def which_booking(lines: Sequence[str]) -> str:
    return "Which booking do you mean?\n" + "\n".join(lines)


def reconcile_booked(booking_id: str) -> str:
    return f"Update on your earlier booking: it {RECONCILED} (ID {booking_id})."


def reconcile_not_booked() -> str:
    return (
        "Update on your earlier booking: it was interrupted before it went through. "
        "Nothing was booked."
    )


# ----------------------------------------------------------------------------- failures


def limit_hit(limit: str) -> str:
    return (
        f"I stopped because this request hit a safety limit ({limit}). Nothing further was "
        "booked or changed. Please try again, or simplify the request."
    )


def tool_unavailable(tool: str) -> str:
    return (
        f"The provider service isn't responding right now ({tool}). Nothing was booked or "
        "changed. Please try again in a moment."
    )


def removed_turn() -> str:
    return "[message removed at your request]"


def unexpected_error() -> str:
    return (
        "Something went wrong on my side. Nothing was booked or changed. Your progress is "
        "saved, so you can try again."
    )


def llm_fallback(state_card: str) -> str:
    return (
        "I'm having trouble understanding right now. You can still reply 'status', "
        "'confirm <code>' or 'cancel <booking id>'.\n" + state_card
    )


def status(lines: Sequence[str]) -> str:
    return "\n".join(lines) if lines else "Nothing in progress right now."


def didnt_understand() -> str:
    return "Sorry, I didn't catch that. What would you like me to arrange?"


def abandoned() -> str:
    return "OK, I've stopped. Nothing was booked."


def policy_refused() -> str:
    return "I can't do that from here. Nothing was booked or changed."
