from __future__ import annotations

from datetime import UTC, datetime

import pytest

from errand.agent.approval import (
    ApprovalRequest,
    Decision,
    classify_confirmation,
    confirm_code_for,
    idempotency_key_for,
)
from errand.domain.time import ist

CODE = "K7P2"
A, AMB, REJ, NO, MISMATCH = (
    Decision.AFFIRM,
    Decision.AMBIGUOUS,
    Decision.REJECT,
    Decision.NOT_APPROVAL,
    Decision.CODE_MISMATCH,
)

PHRASES: list[tuple[str, Decision]] = [
    ("yes", A),
    ("Yes!", A),
    ("YES.", A),
    ("y", A),
    ("yes yes yes", A),
    ("yep 👍", A),
    ("ok", A),
    ("okay", A),
    ("ok book it", A),
    ("book it", A),
    ("go ahead", A),
    ("Go ahead please", A),
    ("haan", A),
    ("haan ji", A),
    ("confirm", A),
    ("CONFIRM K7P2", A),
    ("k7p2", A),
    ("confirm k7p2 please", A),
    ("sure, go ahead", A),
    ("yes please", A),
    ("approve", A),
    ("yes?", AMB),
    ("yes but make it 11am", AMB),
    ("yes, but at 11", AMB),
    ("ok but cheaper", AMB),
    ("yes instead book Friday", AMB),
    ("maybe yes", AMB),
    ("not yes", AMB),
    ("yes, don't book it", AMB),
    ("ok, don’t book yet", AMB),
    ("confirm K7P2 but at 11am", AMB),
    ("yes for ₹1200", AMB),
    ("yes the other provider", AMB),
    ("ok tomorrow", AMB),
    ("yes, actually wait", AMB),
    ("book the 4pm one", AMB),
    ("confirm M3N4", MISMATCH),
    ("yes M3N4", MISMATCH),
    ("no", REJ),
    ("nope", REJ),
    ("no, cancel", REJ),
    ("yesterday", NO),
    ("what's the price?", NO),
    ("tell me about the plumber", NO),
    ("👍", NO),
    ("", NO),
    ("fine", NO),
    ("ha", NO),
    ("ha ha", NO),
]

# The card's action decides which verb confirms it.
ACTION_PHRASES: list[tuple[str, str, Decision]] = [
    ("cancel", "cancel it", A),
    ("cancel", "yes, cancel it", A),
    ("cancel", "yes please cancel", A),
    ("cancel", "yes", A),
    ("cancel", "CONFIRM K7P2", A),
    ("cancel", "book it", NO),
    ("cancel", "book", NO),
    ("cancel", "yes book it", AMB),
    ("cancel", "cancel it at 5pm", AMB),
    ("cancel", "no", REJ),
    ("cancel", "don't cancel", AMB),
    ("reschedule", "move it", A),
    ("reschedule", "yes, reschedule", A),
    ("reschedule", "yes", A),
    ("reschedule", "cancel", REJ),
    ("reschedule", "book it", NO),
    ("reschedule", "move it to 6pm", AMB),
    ("book", "cancel it", REJ),
    ("book", "move it", NO),
]


@pytest.mark.parametrize(("text", "expected"), PHRASES)
def test_confirmation_phrase_table(text: str, expected: Decision) -> None:
    assert classify_confirmation(text, CODE) == expected


@pytest.mark.parametrize(("action", "text", "expected"), ACTION_PHRASES)
def test_confirmation_depends_on_the_cards_action(
    action: str, text: str, expected: Decision
) -> None:
    assert classify_confirmation(text, CODE, action) == expected


def _req(**overrides: object) -> ApprovalRequest:
    base: dict[str, object] = {
        "request_id": "ar_1",
        "action": "book",
        "user_id": "u_demo",
        "provider_id": "pl_aqua",
        "provider_name": "AquaFlow Plumbing",
        "slot": ist(2026, 11, 4, 9),
        "price_paise": 120000,
        "hold_id": "h_000001",
        "nonce": "n1",
        "created_at": datetime(2026, 11, 2, 9, tzinfo=UTC),
    }
    base.update(overrides)
    return ApprovalRequest.model_validate(base)


def test_fingerprint_changes_with_every_material_field() -> None:
    base = _req().fingerprint
    for field, value in [
        ("provider_id", "pl_quickfix"),
        ("slot", ist(2026, 11, 4, 11)),
        ("price_paise", 145000),
        ("hold_id", "h_000002"),
        ("action", "cancel"),
        ("user_id", "u_other"),
        ("nonce", "n2"),
    ]:
        assert _req(**{field: value}).fingerprint != base, field


def test_fingerprint_ignores_display_only_fields() -> None:
    assert _req(previous_price_paise=1).fingerprint == _req().fingerprint
    assert _req(provider_name="renamed").fingerprint == _req().fingerprint


def test_slot_fingerprint_is_timezone_canonical() -> None:
    same_instant_utc = ist(2026, 11, 4, 9).astimezone(UTC)
    assert _req(slot=same_instant_utc).fingerprint == _req().fingerprint


def test_confirm_code_shape_cannot_look_like_time_or_price() -> None:
    for i in range(200):
        code = confirm_code_for(f"{i:064x}")
        assert len(code) == 4
        assert code[0::2].isalpha()
        assert code[1::2].isdigit()


def test_idempotency_key_is_deterministic_per_user_and_fingerprint() -> None:
    fp = _req().fingerprint
    assert idempotency_key_for("u_demo", fp) == idempotency_key_for("u_demo", fp)
    assert idempotency_key_for("u_demo", fp) != idempotency_key_for("u_other", fp)
