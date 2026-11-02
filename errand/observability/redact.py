"""Redact personal data before anything reaches a trace file."""

from __future__ import annotations

import re

from pydantic import JsonValue

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Indian mobile numbers with optional +91 / 0 prefix and common separators.
_PHONE = re.compile(r"(?<!\w)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")


def redact_text(text: str) -> str:
    return _PHONE.sub("[PHONE]", _EMAIL.sub("[EMAIL]", text))


def redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value
