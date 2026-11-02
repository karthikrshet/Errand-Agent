"""Unit tests for forget scrubbing precision (BUGS.md #5)."""

from __future__ import annotations

from errand.memory.forgetting import scrub_text, scrubbable


def test_scrub_is_whole_word_and_case_insensitive() -> None:
    text = "I live in Koramangala; koramangala 4th block. Koramangalam is elsewhere."
    out = scrub_text(text, ["Koramangala"])
    assert out == "I live in [forgotten]; [forgotten] 4th block. Koramangalam is elsewhere."


def test_short_and_numeric_values_are_not_scrubbed_from_free_text() -> None:
    """Regression for the reviewer's repro: forgetting earliest_hour=10 corrupted
    '1050 rupees' and 'b_10abc'."""
    text = "my budget is 1050 rupees and booking b_10abc, never before 10"
    assert scrub_text(text, ["10"]) == text
    assert scrubbable(["10", "ab", "Suresh"]) == ["Suresh"]
