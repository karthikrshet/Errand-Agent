"""Code-level check that a consequential intent came from the user's own words.

The model may misread a message, or obey text injected through a review it was shown. For the
two intents that act on an *existing* booking, the user's raw message must contain a matching
verb before the flow starts. This is deliberately lexical: it cannot be talked around, and a
false negative costs one rephrase ("cancel my AC servicing"), never a wrong cancellation.
"""

from __future__ import annotations

import re

_VERBS: dict[str, re.Pattern[str]] = {
    "cancel_booking": re.compile(
        r"\b(cancel\w*|call(ing)?\s+off|don'?t\s+need|no\s+longer\s+need|scrap|radd?)\b"
    ),
    "reschedule_booking": re.compile(
        r"\b(re-?schedul\w*|move|moving|shift\w*|postpone\w*|prepone\w*|push\w*|change|"
        r"bring\s+forward|re-?book\w*)\b"
    ),
}


def grounded(intent: str, user_text: str) -> bool:
    """True unless `intent` acts on an existing booking and the user's text never asked for it."""
    pattern = _VERBS.get(intent)
    return pattern is None or pattern.search(user_text.lower()) is not None
