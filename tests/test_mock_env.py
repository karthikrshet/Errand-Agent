from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from errand.clock import FakeClock
from errand.domain.money import format_inr, rupees
from errand.domain.time import to_ist
from errand.errors import (
    IdempotencyConflict,
    InvalidRequest,
    PermissionDenied,
    ToolTimeout,
    TransientToolError,
    UnknownResource,
)
from errand.tools import catalog
from errand.tools.contracts import DateRange, ProviderSummary
from errand.tools.mock_env import MockToolEnv
from errand.tools.scenarios import Scenario, get_scenario
from tests.conftest import EnvFactory


def _week(env: MockToolEnv) -> DateRange:
    today = to_ist(env.clock.now()).date()
    return DateRange(start=today, end=today + timedelta(days=6))


def _first(env: MockToolEnv, service: str = "plumber", pid: str = "pl_aqua") -> ProviderSummary:
    out = env.search_providers(service, "Indiranagar", _week(env))
    return next(p for p in out.providers if p.provider_id == pid)


# ---------------------------------------------------------------- money


@pytest.mark.parametrize(
    ("paise", "text"),
    [(0, "₹0"), (150000, "₹1,500"), (145050, "₹1,450.50"), (12500000, "₹1,25,000"), (-500, "-₹5")],
)
def test_format_inr_uses_indian_grouping(paise: int, text: str) -> None:
    assert format_inr(paise) == text


# ---------------------------------------------------------------- search / provider


@pytest.mark.parametrize("service", sorted(catalog.SERVICES))
def test_search_returns_3_to_8_providers_with_contract_fields(
    make_env: EnvFactory, service: str
) -> None:
    env = make_env()
    providers = env.search_providers(service, "HSR Layout", _week(env)).providers
    assert 3 <= len(providers) <= 8
    for p in providers:
        assert isinstance(p.price.paise, int)
        assert p.price.display.startswith("₹")
        assert p.available_slots, "every provider should have slots this week"
        assert all(s.tzinfo is not None and s > env.clock.now() for s in p.available_slots)
        assert p.attributes.technician_name


def test_search_rejects_inverted_or_huge_range(make_env: EnvFactory) -> None:
    env = make_env()
    today = to_ist(env.clock.now()).date()
    with pytest.raises(InvalidRequest):
        env.search_providers("plumber", "x", DateRange(start=today, end=today - timedelta(1)))
    with pytest.raises(InvalidRequest):
        env.search_providers("plumber", "x", DateRange(start=today, end=today + timedelta(60)))


def test_reviews_contain_every_injection_variant(make_env: EnvFactory) -> None:
    env = make_env()
    texts = [r.text for p in catalog.CATALOG for r in env.get_provider(p.provider_id).reviews]
    assert catalog.INJECTION_SYSTEM_OVERRIDE in texts
    assert catalog.INJECTION_ROLE_SPOOF in texts
    assert catalog.INJECTION_FAKE_PRICE in texts
    zero_width = [t for t in texts if "\u200b" in t]
    assert zero_width
    assert "⁠" in zero_width[0]
    assert env.get_provider("pl_trust").provider.name == catalog.INJECTED_PROVIDER_NAME


def test_unknown_provider(make_env: EnvFactory) -> None:
    with pytest.raises(UnknownResource):
        make_env().get_provider("nope")


# ---------------------------------------------------------------- hold


def test_hold_lasts_ten_minutes_and_quotes_search_price(make_env: EnvFactory) -> None:
    env = make_env()
    p = _first(env)
    hold = env.hold_slot(p.provider_id, p.available_slots[0])
    assert hold.expires_at - env.clock.now() == timedelta(minutes=10)
    assert hold.quoted_price == p.price


def test_hold_quote_can_differ_from_search_price(make_env: EnvFactory) -> None:
    env = make_env(get_scenario("price_change"))
    p = _first(env)
    hold = env.hold_slot(p.provider_id, p.available_slots[0])
    assert hold.quoted_price.paise == p.price.paise + 25000


def test_rehold_by_same_user_is_idempotent_but_blocks_others(make_env: EnvFactory) -> None:
    env, other = make_env(), make_env(user_id="u_other")
    p = _first(env)
    h1 = env.hold_slot(p.provider_id, p.available_slots[0])
    assert env.hold_slot(p.provider_id, p.available_slots[0]).hold_id == h1.hold_id
    with pytest.raises(InvalidRequest, match="held by another"):
        other.hold_slot(p.provider_id, p.available_slots[0])
    assert p.available_slots[0] not in _first(other).available_slots


def test_hold_rejects_slot_outside_provider_hours(make_env: EnvFactory) -> None:
    env = make_env()
    slot = _first(env).available_slots[0].replace(hour=3)
    with pytest.raises(InvalidRequest):
        env.hold_slot("pl_aqua", slot)


# ---------------------------------------------------------------- book


def _hold(env: MockToolEnv, idx: int = 0) -> str:
    p = _first(env)
    return env.hold_slot(p.provider_id, p.available_slots[idx]).hold_id


def test_book_success_and_same_key_same_outcome(make_env: EnvFactory) -> None:
    env = make_env(Scenario(book_outcomes=["success", "slot_taken"]))
    hold_id = _hold(env)
    first = env.book(hold_id, "k1")
    again = env.book(hold_id, "k1")
    assert first.outcome == again.outcome == "booked"
    assert first.booking_id == again.booking_id
    assert env.count_bookings("k1") == 1


def test_timeout_after_success_is_booked_and_retry_returns_same_booking(
    make_env: EnvFactory,
) -> None:
    env = make_env(get_scenario("timeout_after_success"))
    hold_id = _hold(env)
    with pytest.raises(ToolTimeout):
        env.book(hold_id, "k1")
    status = env.get_booking_status(idempotency_key="k1")
    assert status.status == "booked"
    assert env.get_booking_status(hold_id=hold_id).booking_id == status.booking_id
    retry = env.book(hold_id, "k1")
    assert retry.outcome == "booked"
    assert retry.booking_id == status.booking_id
    assert env.count_bookings("k1") == 1


def test_timeout_not_booked_then_retry_same_key_books_once(make_env: EnvFactory) -> None:
    env = make_env(get_scenario("timeout_not_booked"))
    hold_id = _hold(env)
    with pytest.raises(ToolTimeout):
        env.book(hold_id, "k1")
    status = env.get_booking_status(idempotency_key="k1")
    assert status.status == "not_booked"
    assert status.hold_expired is False
    assert env.book(hold_id, "k1").outcome == "booked"
    assert env.count_bookings("k1") == 1


def test_slot_taken_is_memoised_and_slot_disappears(make_env: EnvFactory) -> None:
    env = make_env(Scenario(book_outcomes=["slot_taken", "success"]))
    p = _first(env)
    slot = p.available_slots[0]
    hold_id = env.hold_slot(p.provider_id, slot).hold_id
    assert env.book(hold_id, "k1").outcome == "slot_taken"
    assert env.book(hold_id, "k1").outcome == "slot_taken"
    assert slot not in _first(env).available_slots
    assert env.get_booking_status(idempotency_key="k1").status == "not_booked"


def test_hold_expired(make_env: EnvFactory, clock: FakeClock) -> None:
    env = make_env(get_scenario("happy_path"))
    hold_id = _hold(env)
    clock.advance(601)
    assert env.book(hold_id, "k1").outcome == "hold_expired"
    status = env.get_booking_status(hold_id=hold_id)
    assert status.status == "not_booked"
    assert status.hold_expired is True


def test_idempotency_key_reuse_with_other_hold_conflicts(make_env: EnvFactory) -> None:
    env = make_env(Scenario(book_outcomes=["success", "success"]))
    env.book(_hold(env, 0), "k1")
    with pytest.raises(IdempotencyConflict):
        env.book(_hold(env, 1), "k1")


def test_booking_status_requires_exactly_one_lookup_and_reports_unknown(
    make_env: EnvFactory,
) -> None:
    env = make_env()
    assert env.get_booking_status(idempotency_key="never").status == "unknown_hold"
    assert env.get_booking_status(hold_id="h_999999").status == "unknown_hold"
    with pytest.raises(InvalidRequest):
        env.get_booking_status()
    with pytest.raises(InvalidRequest):
        env.get_booking_status(hold_id="a", idempotency_key="b")


def test_other_user_cannot_book_or_see_my_hold(make_env: EnvFactory) -> None:
    env, other = make_env(), make_env(user_id="u_other")
    hold_id = _hold(env)
    with pytest.raises(UnknownResource):
        other.book(hold_id, "k-other")
    assert other.get_booking_status(hold_id=hold_id).status == "unknown_hold"


def test_ledger_survives_restart(make_env: EnvFactory) -> None:
    env = make_env(get_scenario("happy_path"))
    booking = env.book(_hold(env), "k1")
    restarted = make_env()
    assert restarted.get_booking_status(idempotency_key="k1").booking_id == booking.booking_id


# ---------------------------------------------------------------- cancel / reschedule


def test_cancel_first_call_503_then_retry_succeeds_idempotently(make_env: EnvFactory) -> None:
    env = make_env()
    with pytest.raises(TransientToolError) as exc:
        env.cancel("b_up_ac01", "c1")
    assert exc.value.status == 503
    assert env.cancel("b_up_ac01", "c1").outcome == "cancelled"
    assert env.cancel("b_up_ac01", "c1").outcome == "cancelled"
    statuses = {b.booking_id: b.status for b in env.list_bookings("u_demo").bookings}
    assert statuses["b_up_ac01"] == "cancelled"


def test_cannot_cancel_or_reschedule_other_users_booking(make_env: EnvFactory) -> None:
    env = make_env(Scenario(cancel_first_call_503=False))
    with pytest.raises(UnknownResource):
        env.cancel("b_other_01", "c1")
    with pytest.raises(UnknownResource):
        env.reschedule("b_other_01", env.clock.now() + timedelta(days=2), "r1")


def _ac_slots(env: MockToolEnv) -> tuple[ProviderSummary, list[datetime]]:
    p = _first(env, "ac_service", "ac_cool")
    return p, list(p.available_slots)


def test_reschedule_conflict_offers_alternatives(make_env: EnvFactory) -> None:
    env = make_env()
    _, slots = _ac_slots(env)
    conflict = slots[4]
    env.scenario = Scenario(reschedule_conflict_slots=[conflict])
    out = env.reschedule("b_up_ac01", conflict, "r1")
    assert out.outcome == "slot_conflict"
    assert out.alternative_slots
    assert conflict not in out.alternative_slots
    assert env.reschedule("b_up_ac01", conflict, "r1") == out


def test_reschedule_success_moves_booking(make_env: EnvFactory) -> None:
    env = make_env()
    _, slots = _ac_slots(env)
    out = env.reschedule("b_up_ac01", slots[5], "r1")
    assert out.outcome == "rescheduled"
    moved = next(b for b in env.list_bookings("u_demo").bookings if b.booking_id == "b_up_ac01")
    assert moved.slot == slots[5]


# ---------------------------------------------------------------- list / events


def test_seeded_history_supports_brief_examples(make_env: EnvFactory) -> None:
    bookings = make_env().list_bookings("u_demo").bookings
    by_id = {b.booking_id: b for b in bookings}
    assert by_id["b_hist_el01"].status == "completed"
    assert by_id["b_hist_el01"].provider_id == "el_ravi"
    assert by_id["b_up_ac01"].status == "upcoming"
    assert by_id["b_up_ac01"].service == "ac_service"
    assert "b_other_01" not in by_id


def test_list_bookings_for_other_user_denied(make_env: EnvFactory) -> None:
    with pytest.raises(PermissionDenied):
        make_env().list_bookings("u_other")


def test_provider_cancellation_emits_event(make_env: EnvFactory) -> None:
    env = make_env()
    env.simulate_provider_cancellation("b_up_ac01")
    events = env.pending_events()
    assert events
    assert events[0][1:] == ("provider_cancelled", "b_up_ac01")
    env.ack_event(events[0][0])
    assert env.pending_events() == []
    assert make_env(user_id="u_other").pending_events() == []


# ---------------------------------------------------------------- wire API


def test_wire_api_round_trips_and_validates(make_env: EnvFactory) -> None:
    env = make_env()
    out = env.call("get_provider", {"provider_id": "pl_aqua"})
    assert out["provider"]["price"] == {"paise": 120000, "display": "₹1,200"}
    with pytest.raises(InvalidRequest):
        env.call("hold_slot", {"provider_id": "pl_aqua"})
    with pytest.raises(UnknownResource):
        env.call("drop_tables", {})


def test_wire_api_injected_failures(make_env: EnvFactory) -> None:
    env = make_env(
        Scenario(injected_failures={"get_provider": ["malformed", "timeout", "503", "crash"]})
    )
    args = {"provider_id": "pl_aqua"}
    assert "provider" not in env.call("get_provider", args)
    with pytest.raises(ToolTimeout):
        env.call("get_provider", args)
    with pytest.raises(TransientToolError):
        env.call("get_provider", args)
    with pytest.raises(RuntimeError):
        env.call("get_provider", args)
    assert "provider" in env.call("get_provider", args)


def test_price_literal_helper() -> None:
    assert rupees(1500) == 150000
