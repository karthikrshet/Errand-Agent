import importlib
import pkgutil
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import errand
from errand.clock import Clock, FakeClock, SystemClock
from errand.config import BookOutcomeOdds, Settings


def test_every_module_imports() -> None:
    for mod in pkgutil.walk_packages(errand.__path__, prefix="errand."):
        importlib.import_module(mod.name)


def test_settings_defaults_match_brief() -> None:
    s = Settings()
    assert s.max_model_calls_per_turn == 8
    assert s.max_tool_calls_per_run == 25
    assert s.run_wall_clock_s == 60.0
    assert s.hold_ttl_s == 600
    assert (s.book_odds.success, s.book_odds.slot_taken, s.book_odds.timeout) == (
        0.70,
        0.15,
        0.15,
    )
    assert s.book_odds.timeout_booked_fraction == 0.5


def test_effective_budget_applies_safety_margin() -> None:
    assert Settings(context_budget_tokens=3000).effective_context_budget == 2550


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRAND_MAX_TOOL_CALLS_PER_RUN", "3")
    monkeypatch.setenv("ERRAND_BOOK_ODDS__SUCCESS", "0.85")
    monkeypatch.setenv("ERRAND_BOOK_ODDS__TIMEOUT", "0.0")
    s = Settings()
    assert s.max_tool_calls_per_run == 3
    assert s.book_odds.success == 0.85


def test_book_odds_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        BookOutcomeOdds(success=0.9, slot_taken=0.15, timeout=0.15)


def test_retry_policy_falls_back_to_default() -> None:
    s = Settings()
    assert s.retry_for("cancel").max_attempts == 3
    assert s.retry_for("search_providers") == s.default_retry


def test_fake_clock_sleep_advances_virtual_time() -> None:
    start = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    clock = FakeClock(start)
    clock.sleep(1.5)
    clock.advance(600)
    assert clock.now() == start + timedelta(seconds=601.5)
    assert clock.slept == [1.5]


def test_fake_clock_rejects_naive_start_and_negative_sleep() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="negative"):
        FakeClock().sleep(-1)


def test_clocks_satisfy_protocol() -> None:
    clocks: list[Clock] = [SystemClock(), FakeClock()]
    for c in clocks:
        assert c.now().tzinfo is not None
