"""Command-line entry point (`errand ...`)."""

from __future__ import annotations

import typer
from rich.console import Console

from errand.config import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def config() -> None:
    """Print the effective settings (useful when overriding via ERRAND_* env vars)."""
    console.print_json(Settings().model_dump_json())


if __name__ == "__main__":
    app()
