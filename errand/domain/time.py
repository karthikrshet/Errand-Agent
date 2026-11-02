"""Service times are local to India. A fixed +05:30 offset avoids a tzdata dependency on Windows
(IST has no DST, so a fixed offset is exact)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return dt.astimezone(IST)


def ist(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def iso(dt: datetime) -> str:
    """Canonical slot key: IST, minute precision. Used for ledger keys and fingerprints."""
    return to_ist(dt).replace(second=0, microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"slot must include a timezone offset: {value!r}")
    return dt


def days_in_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()
