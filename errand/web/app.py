"""FastAPI app. The chat and live-trace endpoints arrive in Phase 7."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Errand Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
