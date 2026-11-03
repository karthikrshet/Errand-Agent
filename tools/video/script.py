"""The film: scenes, narration (one caption per sentence) and what happens on screen.

Each scene is either a `deck` scene (an animated slide in deck.html, whose elements with
data-s="N" appear when sentence N starts) or an `app` scene (the real web app, driven by an
animated cursor). For app scenes, each sentence carries the actions that run while it is spoken.

Action tuples:
  ("type", text)          click the chat box and type `text`, then press Enter
  ("wait_reply", n)       wait until the chat shows at least n agent messages more than before
  ("click", selector)     move the cursor to an element and click it
  ("hover", selector)     move the cursor to an element
  ("highlight", needle)   highlight trace/chat rows containing `needle`
  ("unhighlight",)        clear highlights
  ("zoom", selector, s)   smooth zoom on an element (s=1 resets)
  ("pause", seconds)
  ("chip", text)          change the scene label in the corner
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLUMBER = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"

Action = tuple[Any, ...]


@dataclass
class Line:
    text: str
    actions: list[Action] = field(default_factory=list)


@dataclass
class Scene:
    sid: str
    kind: str  # "deck" | "app"
    lines: list[Line]
    chip: str = ""
    app_scenario: str = "default"
    tail_s: float = 1.2


SCENES: list[Scene] = [
    Scene(
        "title",
        "deck",
        [
            Line(
                "This is Errand Agent: a chat assistant that books home services, like a plumber, "
                "an electrician, or AC servicing."
            ),
            Line("Its core idea is simple. The language model proposes, and plain code disposes."),
            Line("Let's look at how it works, and then try to break it."),
        ],
    ),
    Scene(
        "arch",
        "deck",
        [
            Line("Here is how a single message flows through the system."),
            Line(
                "First, the agent reconciles any unfinished booking against the provider's source "
                "of truth."
            ),
            Line(
                "If an approval card is waiting, deterministic rules, not the model, decide whether "
                "the reply really is a yes."
            ),
            Line(
                "Otherwise, the language model turns the message into a schema-validated intent, "
                "and plain code runs the workflow."
            ),
            Line(
                "Every tool call passes a gateway that enforces a per-state allowlist, retries with "
                "backoff, and writes a trace."
            ),
            Line(
                "Provider reviews are read only by a quarantined sub-agent that has no tools at all."
            ),
            Line(
                "And memory, with a token-budgeted context builder, decides exactly what the model "
                "may see."
            ),
        ],
    ),
    Scene(
        "guarantees",
        "deck",
        [
            Line("Five guarantees are enforced in code, not by trusting the model."),
            Line(
                "No action without a single-use approval, fingerprinted to the exact provider, "
                "slot, and price."
            ),
            Line("No double booking, thanks to write-ahead intents and idempotency keys."),
            Line("Never booked unless verified at the source of truth."),
            Line("Injected text in reviews can never act."),
            Line("And a forgotten fact never reaches the model again."),
        ],
    ),
    Scene(
        "booking",
        "app",
        chip="Live demo · real app",
        lines=[
            Line(
                "Let's see it live. On the left is the chat, and on the right, the live trace.",
                [("hover", "#text"), ("pause", 0.6)],
            ),
            Line(
                "I ask for a plumber for my kitchen tap, this week, mornings only, under fifteen "
                "hundred rupees.",
                [("type", PLUMBER), ("wait_reply", 1)],
            ),
            Line(
                "The agent asks for one missing detail only: my area.",
                [("highlight", "which area")],
            ),
            Line(
                "I answer, and it searches, ranks three options with reasons, and flags reviews "
                "that contain prompt injection.",
                [
                    ("unhighlight",),
                    ("type", "Indiranagar"),
                    ("wait_reply", 1),
                    ("highlight", "suspicious review"),
                ],
            ),
            Line(
                "Every state transition and tool call streams into the trace as it happens.",
                [
                    ("unhighlight",),
                    ("hover", "#trace"),
                    ("highlight", "security.injection_detected"),
                ],
            ),
            Line(
                "I pick option one. The slot is held, and a confirmation card shows exactly what "
                "will be booked, with a code.",
                [
                    ("unhighlight",),
                    ("type", "book option 1"),
                    ("wait_reply", 1),
                    ("highlight", "Please confirm"),
                ],
            ),
            Line(
                "I reply yes. The approval is granted and consumed in one transaction, and only "
                "after verification does it say booked.",
                [
                    ("unhighlight",),
                    ("type", "yes"),
                    ("wait_reply", 1),
                    ("highlight", "Booked and verified"),
                    ("highlight", "booking.verified"),
                ],
            ),
        ],
    ),
    Scene(
        "timeout",
        "app",
        chip="Live failure · timeout after success",
        app_scenario="timeout_after_success",
        lines=[
            Line(
                "Now let's break it. In this run, the provider's booking call times out, even though "
                "the booking actually went through.",
                [
                    ("type", PLUMBER),
                    ("wait_reply", 1),
                    ("type", "Indiranagar"),
                    ("wait_reply", 1),
                    ("type", "book option 1"),
                    ("wait_reply", 1),
                ],
            ),
            Line("I confirm, exactly as before.", [("type", "yes"), ("wait_reply", 1)]),
            Line(
                "The book call errors with a timeout. The agent does not retry blindly.",
                [("highlight", "ToolTimeout")],
            ),
            Line(
                "It moves to verifying and asks the source of truth first.",
                [("highlight", "VERIFYING"), ("highlight", "get_booking_status")],
            ),
            Line(
                "The booking exists, so it reports success, verified. One booking, never two.",
                [("highlight", "booking.verified"), ("highlight", "Booked and verified")],
            ),
            Line(
                "What if a provider cancels on their own? Here, the AC servicing provider cancels "
                "my upcoming booking.",
                [
                    ("unhighlight",),
                    ("chip", "Proactive follow-up · no user message"),
                    ("click", "#cancelBtn"),
                    ("pause", 0.8),
                ],
            ),
            Line(
                "With no message from me, the watcher detects it from the provider's own records.",
                [("click", "#watchBtn"), ("wait_reply", 1), ("highlight", "proactive.detected")],
            ),
            Line(
                "It excludes the provider who cancelled, finds a replacement within my preferences, "
                "and holds the slot.",
                [("highlight", "I found a replacement")],
            ),
            Line(
                "And it asks for fresh approval. The original approval never carries over.",
                [("highlight", "Please confirm")],
            ),
        ],
    ),
    Scene(
        "evals",
        "deck",
        [
            Line("Every claim is backed by evaluations that run fully offline."),
            Line(
                "Forty-three scenarios with simulated users, plus three replays of real Claude runs, "
                "all pass."
            ),
            Line(
                "Seven invariants are recomputed from the trace and both databases after every "
                "scenario, and mutation tests prove they catch broken code."
            ),
            Line("Nine real bugs are documented, each guarded by a regression test."),
        ],
    ),
    Scene(
        "outro",
        "deck",
        [
            Line(
                "Errand Agent. Safety enforced in code, every failure traced, every claim tested."
            ),
            Line("Thanks for watching."),
        ],
        tail_s=2.0,
    ),
]
