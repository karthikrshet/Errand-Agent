"""Scripted NLU for the keyless demo (`ERRAND_SCRIPT=demo`): exact demo phrases only.

Dates are computed from today so the demo works on any day. Anything not listed degrades to
the deterministic fallback, which is itself part of the story (LLM outage handling).
"""

from __future__ import annotations

from datetime import date, timedelta

from errand.llm.script import nlu, select


def _next(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def demo_script(today: date) -> dict[str, str]:
    sunday = today + timedelta(days=6 - today.weekday())
    saturday = _next(today, 5)
    week = {"date_start": today.isoformat(), "date_end": sunday.isoformat()}
    return {
        "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500": nlu(
            "request",
            service="plumber",
            job="kitchen tap",
            earliest_hour=8,
            latest_hour=12,
            budget_rupees=1500,
            **week,
        ),
        "Indiranagar": nlu("request", area="Indiranagar"),
        "I'm in Indiranagar, remember that": nlu("request", area="Indiranagar", remember=True),
        "book option 1": select(1),
        "book option 2": select(2),
        "book option 3": select(3),
        "Move my AC servicing to Saturday": nlu(
            "reschedule_booking",
            booking_ref={"service": "ac_service"},
            reschedule_to={"date": saturday.isoformat()},
        ),
        "cancel my AC servicing": nlu("cancel_booking", booking_ref={"service": "ac_service"}),
        "Book the same electrician as last time, but not before 10 a.m.": nlu(
            "request",
            service="electrician",
            same_provider_as_last=True,
            earliest_hour=10,
            area="Indiranagar",
            **week,
        ),
        "what do you remember about me": nlu("memory_show"),
        "status": nlu("status"),
    }
