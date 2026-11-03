"""Untrusted free text from tools: normalise, strip invisible characters, truncate, and detect
likely prompt injection.

Flagged text is *kept* (users may still want to read a review) but marked, traced as
`security.injection_detected`, and never placed in a model prompt raw. Detection is a heuristic
alarm, not the defence: the defence is that tool output cannot create grants or reach
user-reserved states, whatever it says.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel

# Zero-width, bidi-control and isolate, word-joiner, BOM, soft-hyphen and Unicode "tag"
# code points (built from ints so the source itself contains no invisible characters). Tags
# (U+E0000-E007F) can spell out a whole hidden ASCII instruction that renders as nothing.
_INVISIBLE_RANGES = (
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0x180E, 0x180E),
    (0xFEFF, 0xFEFF),
    (0xAD, 0xAD),
    (0xE0000, 0xE007F),
)
_INVISIBLE = re.compile(
    "[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _INVISIBLE_RANGES) + "]"
)
ELLIPSIS = chr(0x2026)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("".join(parts), re.IGNORECASE)


INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_marker", _rx(r"(^|\n|\s)(system|assistant|user|developer)\s*:")),
    ("tag_escape", _rx(r"</?\s*(untrusted_data|memory_data|system|instructions?)\s*>")),
    (
        "ignore_previous",
        _rx(
            r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all)\b",
            r".{0,20}\b(instructions?|rules?|prompts?)",
        ),
    ),
    (
        "fake_approval",
        _rx(
            r"\b(pre-?approved|already (confirmed|approved)|without asking",
            r"|approval (is )?(no longer )?(not )?required)\b",
        ),
    ),
    (
        "tool_call",
        _rx(r"\b(book|cancel|reschedule|hold_slot|get_booking_status)\s*\(|\bcall (book|cancel)\b"),
    ),
    ("price_override", _rx(r"price is now|₹\s*0(?!\d)|\bfree\b.{0,20}\ball users\b")),
    ("url", _rx(r"https?://|www\.")),
)


class SanitizedText(BaseModel):
    text: str
    flags: list[str]
    had_invisible_chars: bool
    truncated: bool

    @property
    def injection_suspected(self) -> bool:
        return bool(self.flags)


def sanitize(raw: str, max_chars: int = 500) -> SanitizedText:
    normalized = unicodedata.normalize("NFKC", raw)
    stripped = _CONTROL.sub("", _INVISIBLE.sub("", normalized))
    had_invisible = stripped != normalized
    # Detect on the stripped text so zero-width splitting cannot hide keywords.
    flags = [name for name, pattern in INJECTION_PATTERNS if pattern.search(stripped)]
    if had_invisible:
        flags.append("hidden_characters")
    truncated = len(stripped) > max_chars
    text = stripped[:max_chars] + (ELLIPSIS if truncated else "")
    return SanitizedText(
        text=text, flags=flags, had_invisible_chars=had_invisible, truncated=truncated
    )
