"""Every tunable number in the system lives here, typed and overridable via `ERRAND_*` env vars.

Keeping limits, probabilities and budgets in one object means tests and eval scenarios can
tighten a single knob (e.g. `max_tool_calls_per_run=3`) without monkeypatching modules.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetryPolicy(BaseModel):
    """Capped exponential backoff with seeded jitter. Only retryable errors are retried."""

    max_attempts: int = Field(default=3, ge=1)
    base_delay_s: float = Field(default=0.2, ge=0)
    max_delay_s: float = Field(default=2.0, ge=0)
    jitter_ratio: float = Field(default=0.25, ge=0, le=1)


class BookOutcomeOdds(BaseModel):
    """Default (non-forced) outcome distribution for `book`, straight from the brief."""

    success: float = 0.70
    slot_taken: float = 0.15
    timeout: float = 0.15
    timeout_booked_fraction: float = 0.5

    @model_validator(mode="after")
    def _sums_to_one(self) -> BookOutcomeOdds:
        total = self.success + self.slot_taken + self.timeout
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"book outcome odds must sum to 1.0, got {total}")
        return self


class ModelPrice(BaseModel):
    """USD per million tokens, used only for the trace's cost *estimate*."""

    input_per_mtok: float
    output_per_mtok: float


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", env_nested_delimiter="__")

    # Identity: the acting user comes from session config, never from the LLM or tool output.
    user_id: str = "u_demo"

    # Determinism
    seed: int = 42

    # Storage
    db_path: Path = Path("errand.db")
    traces_dir: Path = Path("traces")

    # LLM
    model: str = "claude-opus-5-5"
    llm_max_retries: int = 2
    llm_repair_attempts: int = 1
    llm_timeout_s: float = 30.0
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)

    # Hard limits (bounded execution)
    max_model_calls_per_turn: int = 8
    max_tool_calls_per_run: int = 25
    run_wall_clock_s: float = 60.0
    tool_timeout_s: float = 5.0

    # Retries
    default_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    tool_retry: dict[str, RetryPolicy] = Field(
        default_factory=lambda: {
            "cancel": RetryPolicy(max_attempts=3),
            "book": RetryPolicy(max_attempts=2),
            "reschedule": RetryPolicy(max_attempts=3),
        }
    )

    # Mock provider world
    hold_ttl_s: int = 600
    book_odds: BookOutcomeOdds = Field(default_factory=BookOutcomeOdds)
    search_min_providers: int = 3
    search_max_providers: int = 8

    # Context management
    context_budget_tokens: int = 3000
    token_safety_margin: float = Field(default=0.15, ge=0, lt=1)
    recent_turns: int = 6
    # Re-summarise once this many turns have aged out of the recent window.
    summary_batch_turns: int = 20

    # Untrusted text handling
    untrusted_max_chars: int = 500

    @property
    def effective_context_budget(self) -> int:
        """tiktoken only approximates Anthropic tokenisation, so we under-fill on purpose."""
        return int(self.context_budget_tokens * (1 - self.token_safety_margin))

    def retry_for(self, tool_name: str) -> RetryPolicy:
        return self.tool_retry.get(tool_name, self.default_retry)
