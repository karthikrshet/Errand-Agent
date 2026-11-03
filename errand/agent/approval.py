"""Approval requests, fingerprints and the deterministic confirmation rules.

No LLM is involved in deciding whether a user message approves an action. The model may help
explain a reply, but a grant is only ever produced by `classify_confirmation` returning AFFIRM
for the *latest user message* while a matching request is pending.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand.domain.time import iso

Action = Literal["book", "cancel", "reschedule"]

# Confirm codes are letter-digit-letter-digit, so they can never be mistaken for a time or price.
_CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_DIGITS = "23456789"


class ApprovalRequest(BaseModel):
    """Created by code, never by the LLM. Every field shown to the user comes from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    action: Action
    user_id: str
    provider_id: str
    provider_name: str
    slot: datetime
    price_paise: int
    hold_id: str | None = None
    booking_id: str | None = None
    nonce: str
    created_at: datetime
    # Display-only context for diffs ("Price changed from ... to ..."); not part of the fingerprint.
    previous_price_paise: int | None = None
    previous_slot: datetime | None = None
    # Raised by the watcher with no user message: the user may answer "yes" to something else
    # before they have seen it, so only the card's own code confirms it.
    unprompted: bool = False

    def canonical(self) -> dict[str, str | int | None]:
        return {
            "action": self.action,
            "provider_id": self.provider_id,
            "slot": iso(self.slot),
            "price_paise": self.price_paise,
            "hold_id": self.hold_id,
            "booking_id": self.booking_id,
            "user_id": self.user_id,
            "nonce": self.nonce,
            "created_at": self.created_at.isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def confirm_code(self) -> str:
        return confirm_code_for(self.fingerprint)


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    request_id: str
    fingerprint: str
    user_id: str
    issued_at: datetime
    # Grants can only originate from the user-message channel; kept explicit for the trace.
    source: Literal["user_message"] = "user_message"


def confirm_code_for(fingerprint: str) -> str:
    n = int(fingerprint[:16], 16)
    chars = []
    for i in range(4):
        alphabet = _CODE_LETTERS if i % 2 == 0 else _CODE_DIGITS
        n, r = divmod(n, len(alphabet))
        chars.append(alphabet[r])
    return "".join(chars)


def idempotency_key_for(user_id: str, fingerprint: str) -> str:
    """Deterministic: repeated confirmations, retries and restarts all reuse the same key."""
    return "idem_" + hashlib.sha256(f"{user_id}:{fingerprint}".encode()).hexdigest()[:24]


# ----------------------------------------------------------------------------- confirmation


class Decision(StrEnum):
    AFFIRM = "affirm"
    CODE_REQUIRED = "code_required"
    AMBIGUOUS = "ambiguous"
    REJECT = "reject"
    CODE_MISMATCH = "code_mismatch"
    NOT_APPROVAL = "not_approval"


AFFIRM_PHRASES: frozenset[str] = frozenset(
    {
        "yes", "y", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "book it", "go ahead",
        "haan", "haan ji", "ji haan", "ok", "okay", "ok book", "ok book it", "yes please",
        "please book", "do it", "proceed", "approve", "approved", "yes book it", "yes go ahead",
        "yes confirm", "confirm booking", "book", "sounds good", "theek hai", "go for it",
        "yes do it", "ok go ahead", "okay go ahead", "yes proceed", "please proceed",
    }
)  # fmt: skip
_STRONG = frozenset(
    {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "haan",
     "proceed", "approve", "approved", "book"}
)  # fmt: skip
_FILLER = frozenset({"it", "go", "ahead", "please", "ji", "do", "hai", "theek", "now", "that"})
_POLITE = frozenset({"please", "ji", "now", "pls", "plz"})
_REJECT = frozenset({"no", "nope", "nah", "nahi", "na", "cancel", "stop", "dont", "abort"})
_NEGATION = frozenset({"no", "not", "dont", "never", "nahi", "nope", "cancel", "stop", "wait"})
_CHANGERS = frozenset(
    {"but", "maybe", "instead", "change", "different", "other", "another", "cheaper", "later",
     "earlier", "tomorrow", "today", "morning", "evening", "afternoon", "noon", "night", "if",
     "rs", "rupees", "inr", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
     "sunday", "am", "pm", "except", "unless", "only", "actually", "hmm", "perhaps"}
)  # fmt: skip
_NUMBERISH = re.compile(r"\d")

# A card names one action, and only that action's verb confirms it: on a cancel card "cancel
# it" is a yes and "book" is a change of mind; on a book card "cancel" is a no.
_ACTION_VERBS: dict[str, frozenset[str]] = {
    "book": frozenset({"book"}),
    "cancel": frozenset({"cancel"}),
    "reschedule": frozenset({"reschedule", "move"}),
}
_ACTION_PHRASES: dict[str, frozenset[str]] = {
    "book": frozenset(),
    "cancel": frozenset(
        {"cancel", "cancel it", "yes cancel", "yes cancel it", "cancel booking",
         "cancel the booking", "yes please cancel", "go ahead and cancel", "ok cancel",
         "ok cancel it"}
    ),
    "reschedule": frozenset(
        {"reschedule", "reschedule it", "yes reschedule", "yes reschedule it", "move it",
         "yes move it", "ok move it", "go ahead and move it"}
    ),
}  # fmt: skip


@dataclass(frozen=True)
class _Rules:
    phrases: frozenset[str]
    strong: frozenset[str]
    reject: frozenset[str]
    negation: frozenset[str]
    changers: frozenset[str]


def _rules(action: str) -> _Rules:
    own = _ACTION_VERBS.get(action, _ACTION_VERBS["book"])
    others = frozenset().union(*(v for k, v in _ACTION_VERBS.items() if k != action)) - own
    book_phrases = AFFIRM_PHRASES if action == "book" else frozenset(
        p for p in AFFIRM_PHRASES if "book" not in p.split()
    )  # fmt: skip
    return _Rules(
        phrases=book_phrases | _ACTION_PHRASES.get(action, frozenset()),
        strong=(_STRONG - others) | own,
        reject=_REJECT - own,
        negation=_NEGATION - own,
        changers=_CHANGERS | others,
    )


_CODE_TOKEN = re.compile(r"^[a-z][2-9][a-z][2-9]$")


def _tokens(text: str) -> list[str]:
    lowered = text.lower().replace(chr(0x2019), "'").replace("n't", " not")
    tokens = re.findall(r"[a-z0-9]+", lowered)
    collapsed: list[str] = []
    for t in tokens:
        if not collapsed or collapsed[-1] != t:
            collapsed.append(t)
    return collapsed


def classify_confirmation(text: str, expected_code: str, action: str = "book") -> Decision:  # noqa: PLR0911
    """Deterministic rules from the brief: closed allowlist, or echo of the confirm code.

    Anything that could change the action (negation, a time, a price, a question, 'but',
    'instead', ...) next to an affirmative is AMBIGUOUS and never grants. `action` is the
    card's action, so only its own verb confirms it.
    """
    rules = _rules(action)
    has_question = "?" in text or "₹" in text
    tokens = _tokens(text)
    if not tokens:
        return Decision.NOT_APPROVAL
    code = expected_code.lower()
    codes_in_msg = [t for t in tokens if _CODE_TOKEN.match(t)]
    if codes_in_msg and code not in codes_in_msg:
        return Decision.CODE_MISMATCH
    has_code = code in tokens
    rest = [t for t in tokens if t not in (code, "confirm")] if has_code else tokens
    risky = has_question or any(
        t in rules.negation or t in rules.changers or _NUMBERISH.search(t) for t in rest
    )
    has_affirm = has_code or any(t in rules.strong for t in tokens)

    if has_affirm and risky:
        return Decision.AMBIGUOUS
    if has_code:
        return (
            Decision.AFFIRM
            if all(t in rules.strong or t in _FILLER for t in rest)
            else (Decision.AMBIGUOUS)
        )
    if all(t in rules.reject or t in _FILLER for t in tokens) and any(
        t in rules.reject for t in tokens
    ):
        return Decision.REJECT
    if " ".join(t for t in tokens if t not in _POLITE) in rules.phrases:
        return Decision.AFFIRM
    if has_affirm and all(t in rules.strong or t in _FILLER for t in tokens):
        return Decision.AFFIRM
    if has_affirm:
        return Decision.AMBIGUOUS
    return Decision.NOT_APPROVAL


def classify_for(req: ApprovalRequest, text: str) -> Decision:
    """The decision for a reply to this specific card: the generic rules, plus the code rule
    for cards the watcher raised on its own."""
    decision = classify_confirmation(text, req.confirm_code, req.action)
    code_missing = req.confirm_code.lower() not in _tokens(text)
    if decision == Decision.AFFIRM and req.unprompted and code_missing:
        return Decision.CODE_REQUIRED
    return decision
