"""Token counting for budget enforcement.

tiktoken's cl100k_base only approximates Anthropic tokenisation, so the budget applies a safety
margin (see `Settings.effective_context_budget`). If the encoding file cannot be loaded (fully
offline machine), we fall back to a deliberately pessimistic chars/3 estimate: over-counting
only shrinks the context, it can never blow the budget.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import lru_cache


@lru_cache(maxsize=1)
def _encoder() -> tuple[str, Callable[[str], int]]:
    try:
        import tiktoken  # noqa: PLC0415

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken:cl100k_base", lambda s: len(enc.encode(s, disallowed_special=()))
    except Exception:
        return "heuristic:chars/3", lambda s: math.ceil(len(s) / 3)


def count_tokens(text: str) -> int:
    return _encoder()[1](text)


def counter_name() -> str:
    return _encoder()[0]
