"""Cross-platform task runner. The Makefile delegates here so Windows users without `make`
can run the same targets: `python tasks.py check`."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

PY = sys.executable


def _run(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def lint() -> None:
    _run(PY, "-m", "ruff", "check", ".")
    _run(PY, "-m", "ruff", "format", "--check", ".")


def typecheck() -> None:
    _run(PY, "-m", "mypy")


def check() -> None:
    lint()
    typecheck()


def fmt() -> None:
    _run(PY, "-m", "ruff", "format", ".")
    _run(PY, "-m", "ruff", "check", "--fix", ".")


def test() -> None:
    _run(PY, "-m", "pytest")


def eval_() -> None:
    _run(PY, "-m", "evals.report")


def ui() -> None:
    _run(PY, "-m", "uvicorn", "errand.web.app:app", "--port", "8000")


TASKS: dict[str, Callable[[], None]] = {
    "lint": lint,
    "typecheck": typecheck,
    "check": check,
    "fmt": fmt,
    "test": test,
    "eval": eval_,
    "ui": ui,
}


def main(argv: list[str]) -> None:
    if not argv or any(a not in TASKS for a in argv):
        print(f"usage: python tasks.py [{'|'.join(TASKS)}] ...")
        sys.exit(2)
    for name in argv:
        TASKS[name]()


if __name__ == "__main__":
    main(sys.argv[1:])
