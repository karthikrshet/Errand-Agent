"""Pydantic I/O schemas for the eight tools. Names and shapes are fixed by the brief.

Convention: *business* outcomes (slot_taken, hold_expired, slot_conflict) are returned as data
so callers must handle them explicitly; *transport* failures (timeout, 503) are raised as typed
errors because the caller cannot know whether the side effect happened.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.money import Price

ToolName = Literal[
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
]
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({"book", "cancel", "reschedule"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- search_providers ---


class DateRange(_Frozen):
    start: date
    end: date


class SearchProvidersInput(_Frozen):
    service: str
    area: str
    date_range: DateRange


class ProviderAttributes(_Frozen):
    verified: bool
    languages: tuple[str, ...]
    years_experience: int
    technician_name: str


class ProviderSummary(_Frozen):
    provider_id: str
    name: str
    service: str
    price: Price
    rating: float = Field(ge=0, le=5)
    available_slots: tuple[datetime, ...]
    attributes: ProviderAttributes


class SearchProvidersOutput(_Frozen):
    providers: tuple[ProviderSummary, ...]


# --- get_provider ---


class GetProviderInput(_Frozen):
    provider_id: str


class Review(_Frozen):
    author: str
    rating: int = Field(ge=1, le=5)
    text: str


class GetProviderOutput(_Frozen):
    provider: ProviderSummary
    description: str
    reviews: tuple[Review, ...]


# --- hold_slot ---


class HoldSlotInput(_Frozen):
    provider_id: str
    slot: datetime


class HoldSlotOutput(_Frozen):
    hold_id: str
    provider_id: str
    slot: datetime
    expires_at: datetime
    quoted_price: Price


# --- book ---


class BookInput(_Frozen):
    hold_id: str
    idempotency_key: str


class BookOutput(_Frozen):
    outcome: Literal["booked", "slot_taken", "hold_expired"]
    booking_id: str | None = None
    hold_id: str


# --- get_booking_status ---


class GetBookingStatusInput(_Frozen):
    hold_id: str | None = None
    idempotency_key: str | None = None


class BookingStatusOutput(_Frozen):
    status: Literal["booked", "not_booked", "unknown_hold"]
    booking_id: str | None = None
    hold_expired: bool | None = None


# --- cancel ---


class CancelInput(_Frozen):
    booking_id: str
    idempotency_key: str


class CancelOutput(_Frozen):
    outcome: Literal["cancelled"]
    booking_id: str


# --- reschedule ---


class RescheduleInput(_Frozen):
    booking_id: str
    slot: datetime
    idempotency_key: str


class RescheduleOutput(_Frozen):
    outcome: Literal["rescheduled", "slot_conflict"]
    booking_id: str
    slot: datetime
    alternative_slots: tuple[datetime, ...] = ()


# --- list_bookings ---

BookingStatus = Literal["upcoming", "completed", "cancelled", "cancelled_by_provider"]


class ListBookingsInput(_Frozen):
    user_id: str


class BookingRecord(_Frozen):
    booking_id: str
    provider_id: str
    provider: str
    service: str
    slot: datetime
    status: BookingStatus
    price: Price


class ListBookingsOutput(_Frozen):
    bookings: tuple[BookingRecord, ...]
