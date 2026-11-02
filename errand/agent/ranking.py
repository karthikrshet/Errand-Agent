"""Deterministic filtering and ranking of providers.

Ranking is code, not LLM prose, so "why this provider?" is explained from structured scores and
the same inputs always give the same order. When nothing fits, we report which single constraint
excluded the most options and what relaxing it would unlock.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime

from pydantic import BaseModel

from errand.agent.state import Candidate
from errand.domain.money import format_inr
from errand.domain.request import ServiceRequest
from errand.domain.time import to_ist
from errand.tools.contracts import ProviderSummary

W_RATING, W_PRICE, W_SOONEST = 0.5, 0.3, 0.2
PREFERRED_BONUS = 1.0
MAX_OPTIONS = 3


class RankResult(BaseModel):
    candidates: list[Candidate]
    excluded: dict[str, int]
    reason: str = ""
    suggestion: str = ""


def _window(req: ServiceRequest) -> tuple[date | None, date | None]:
    return req.date_start, req.date_end or req.date_start


def fits_time(slot: datetime, req: ServiceRequest) -> bool:
    local = to_ist(slot)
    start, end = _window(req)
    if start is not None and local.date() < start:
        return False
    if end is not None and local.date() > end:
        return False
    if req.earliest_hour is not None and local.hour < req.earliest_hour:
        return False
    return not (req.latest_hour is not None and local.hour >= req.latest_hour)


def rank(
    providers: Sequence[ProviderSummary],
    req: ServiceRequest,
    *,
    now: datetime,
) -> RankResult:
    excluded: Counter[str] = Counter()
    over_budget: list[int] = []
    fitting: list[tuple[ProviderSummary, list[datetime]]] = []
    for p in providers:
        if p.provider_id in req.blocked_provider_ids:
            excluded["blocked"] += 1
            continue
        if req.preferred_provider_id and p.provider_id != req.preferred_provider_id:
            excluded["not_preferred"] += 1
            continue
        slots = [s for s in p.available_slots if s > now and fits_time(s, req)]
        if not slots:
            excluded["time"] += 1
            continue
        if req.budget_paise is not None and p.price.paise > req.budget_paise:
            excluded["budget"] += 1
            over_budget.append(p.price.paise)
            continue
        fitting.append((p, slots))

    if not fitting:
        reason, suggestion = _explain(excluded, req, over_budget)
        return RankResult(
            candidates=[], excluded=dict(excluded), reason=reason, suggestion=suggestion
        )

    max_price = max(p.price.paise for p, _ in fitting)
    min_price = min(p.price.paise for p, _ in fitting)
    earliest = min(slots[0] for _, slots in fitting)
    latest = max(slots[0] for _, slots in fitting)
    scored = [
        _candidate(p, slots, req, (min_price, max_price), (earliest, latest))
        for p, slots in fitting
    ]
    scored.sort(key=lambda c: (-c.score, c.price_paise, c.provider_id))
    return RankResult(candidates=scored[:MAX_OPTIONS], excluded=dict(excluded))


def _candidate(
    p: ProviderSummary,
    slots: list[datetime],
    req: ServiceRequest,
    price_range: tuple[int, int],
    slot_range: tuple[datetime, datetime],
) -> Candidate:
    lo, hi = price_range
    price_score = 1.0 if hi == lo else (hi - p.price.paise) / (hi - lo)
    first, last = slot_range
    span = (last - first).total_seconds()
    soon_score = 1.0 if span == 0 else 1 - (slots[0] - first).total_seconds() / span
    score = W_RATING * p.rating / 5 + W_PRICE * price_score + W_SOONEST * soon_score
    reasons = [f"rated {p.rating:.1f}/5"]
    if req.budget_paise is not None:
        under = req.budget_paise - p.price.paise
        reasons.append(f"{format_inr(under)} under your {format_inr(req.budget_paise)} budget")
    elif price_score == 1.0:
        reasons.append("lowest price of the matches")
    if soon_score == 1.0:
        reasons.append("earliest available slot")
    if p.attributes.verified:
        reasons.append(f"verified, {p.attributes.years_experience} yrs experience")
    if req.preferred_provider_id == p.provider_id:
        score += PREFERRED_BONUS
        reasons.append("the provider you asked for")
    return Candidate(
        provider_id=p.provider_id,
        name=p.name,
        price_paise=p.price.paise,
        rating=p.rating,
        slot=slots[0],
        other_slots=slots[1:6],
        score=round(score, 4),
        reasons=reasons,
    )


def _explain(
    excluded: Counter[str], req: ServiceRequest, over_budget: list[int]
) -> tuple[str, str]:
    if excluded.get("budget") and req.budget_paise is not None:
        cheapest = min(over_budget)
        return (
            f"every option that fits your time is above your {format_inr(req.budget_paise)} budget",
            f"The cheapest is {format_inr(cheapest)}. Want me to raise the budget to "
            f"{format_inr(cheapest)}?",
        )
    if req.preferred_provider_id and excluded.get("not_preferred"):
        return (
            "the provider you asked for has nothing free in that window",
            "Want me to show other providers instead?",
        )
    if excluded.get("time"):
        return (
            "no provider has a free slot in your date and time window",
            "Want me to widen the time window or look at later dates?",
        )
    return ("no providers matched", "Want to try a different service or area?")
