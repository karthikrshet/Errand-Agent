"""Mutation tests: break the agent on purpose and prove the global invariants catch it."""

from __future__ import annotations

from pathlib import Path

import pytest

from errand.agent import approval_flow, booking
from errand.agent.approval import Decision
from errand.memory import store as memory_store
from errand.tools import contracts as c
from evals.harness import execute
from evals.invariants import check_all
from evals.scenarios import all_scenarios


def _scenario(sid: str):  # type: ignore[no-untyped-def]
    return next(s for s in all_scenarios() if s.sid == sid)


def test_i4_catches_unconfirmed_memory_in_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = memory_store._exclusion_reason

    def leaky(m, request):  # type: ignore[no-untyped-def]
        return None if m.status == "pending_confirmation" else real(m, request)

    monkeypatch.setattr(memory_store, "_exclusion_reason", leaky)
    run = execute(_scenario("22"), tmp_path / "r")
    assert any("forbidden memories" in p for p in check_all(run)["I4"])


def test_i6_catches_grant_from_non_affirming_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_flow, "classify_confirmation", lambda *_: Decision.AFFIRM)
    run = execute(_scenario("33"), tmp_path / "r")
    assert any("non-affirming" in p for p in check_all(run)["I6"])


def test_i3_catches_unverified_success_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lying_status(ctx, intent):  # type: ignore[no-untyped-def]
        return c.BookingStatusOutput(status="booked", booking_id="b_made_up")

    monkeypatch.setattr(booking, "_status", lying_status)
    run = execute(_scenario("11"), tmp_path / "r")
    assert any("not in ledger" in p for p in check_all(run)["I3"])
