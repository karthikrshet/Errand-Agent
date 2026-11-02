"""Command-line entry point (`errand ...`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from errand.app import build_world
from errand.config import Settings
from errand.llm.base import LLMClient
from errand.llm.scripted import ScriptedClient
from errand.observability.trace import TraceEvent, load_trace
from errand.tools.scenarios import get_scenario

app = typer.Typer(no_args_is_help=True, add_completion=False)
trace_app = typer.Typer(no_args_is_help=True)
app.add_typer(trace_app, name="trace")
console = Console()


def _llm(settings: Settings, script: Path | None) -> LLMClient:
    if script is not None:
        mapping = json.loads(script.read_text(encoding="utf-8"))
        return ScriptedClient.from_map(mapping)
    from errand.llm.anthropic_client import AnthropicClient  # noqa: PLC0415

    return AnthropicClient(settings)


@app.command()
def config() -> None:
    """Print the effective settings (useful when overriding via ERRAND_* env vars)."""
    console.print_json(Settings().model_dump_json())


@app.command()
def chat(
    scenario: Annotated[str, typer.Option(help="Mock-world scenario name.")] = "default",
    session: Annotated[str, typer.Option(help="Session id (state persists).")] = "cli",
    script: Annotated[
        Path | None, typer.Option(help="JSON map of message -> scripted NLU output.")
    ] = None,
) -> None:
    """Chat with the agent. Uses the Anthropic API unless --script is given."""
    settings = Settings()
    trace_path = settings.traces_dir / f"{session}.jsonl"
    world = build_world(
        settings=settings,
        llm=_llm(settings, script),
        scenario=get_scenario(scenario),
        trace_path=trace_path,
        run_id=session,
    )
    console.print(Panel(world.agent.startup(session).text, title="agent"))
    console.print(f"[dim]trace: {trace_path}  (Ctrl+C to quit)[/dim]")
    while True:
        try:
            text = console.input("[bold cyan]you> [/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break
        if text.strip():
            reply = world.agent.handle_message(session, text)
            console.print(Panel(reply.text, title=f"agent · {reply.state.status.value}"))


def _line(e: TraceEvent) -> str:
    d = e.data
    if e.kind == "tool.call":
        return (
            f"{d.get('tool')} attempt={d.get('attempt')} -> {d.get('outcome')} "
            f"({d.get('latency_ms')}ms)"
        )
    if e.kind == "state.transition":
        return (
            f"{d.get('from_state')} -> {d.get('to_state')} [{d.get('trigger')}] {d.get('reason')}"
        )
    if e.kind == "model.call":
        return f"{d.get('purpose')} in={d.get('input_tokens')} out={d.get('output_tokens')}"
    keys = (
        "reason",
        "decision",
        "request_id",
        "confirm_code",
        "grant_id",
        "booking_id",
        "idempotency_key",
        "flags",
        "channel",
        "limit",
        "user_text",
        "outcome",
    )
    return " ".join(f"{k}={d[k]}" for k in keys if k in d)


@trace_app.command("show")
def trace_show(path: Path) -> None:
    """Pretty-print a JSONL trace."""
    colours = {"security": "red", "approval": "yellow", "booking": "green", "limit": "magenta"}
    for e in load_trace(path):
        colour = colours.get(e.kind.split(".")[0], "white")
        console.print(f"[dim]{e.seq:>4}[/dim] [{colour}]{e.kind:<28}[/{colour}] {escape(_line(e))}")


if __name__ == "__main__":
    app()
