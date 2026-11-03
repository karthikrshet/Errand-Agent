"""Regression for BUGS #10: one World driven from several threads, as the web app does."""

from __future__ import annotations

import threading
from pathlib import Path

from errand.agent import responses as r
from errand.app import build_world
from errand.config import Settings
from errand.llm.scripted import ScriptedClient
from errand.tools.scenarios import get_scenario
from evals.scenarios.common import PLUMBER_NLU, PLUMBER_TEXT


def test_concurrent_sessions_and_watcher_share_one_world(tmp_path: Path) -> None:
    world = build_world(
        settings=Settings(),
        llm=ScriptedClient(lambda req: PLUMBER_NLU),
        scenario=get_scenario("default"),
        db_path=tmp_path / "agent.db",
        trace_path=tmp_path / "t.jsonl",
    )
    replies: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def chat(sid: str) -> None:
        for _ in range(6):
            try:
                text = world.agent.handle_message(sid, PLUMBER_TEXT).text
            except BaseException as exc:  # pragma: no cover - the assertion reports it
                errors.append(exc)
                continue
            with lock:
                replies.append(text)

    def watch() -> None:
        for _ in range(12):
            for sid in ("s0", "s1", "s2"):
                try:
                    world.agent.proactive_check(sid)
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

    threads = [threading.Thread(target=chat, args=(f"s{i}",)) for i in range(3)]
    threads.append(threading.Thread(target=watch))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(replies) == 18
    assert r.unexpected_error() not in replies
