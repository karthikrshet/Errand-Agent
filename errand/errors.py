"""Typed errors. Control flow branches on exception *types*, never on message strings."""

from __future__ import annotations


class ErrandError(Exception):
    """Base for every error the system raises deliberately."""


# --- tool-level (raised by the mock environment / gateway) ---


class ToolError(ErrandError):
    retryable: bool = False


class ToolTimeout(ToolError):
    """The call may or may not have taken effect. Callers must consult the source of truth."""

    retryable = True


class TransientToolError(ToolError):
    retryable = True

    def __init__(self, status: int = 503, message: str = "service unavailable") -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


class UnknownResource(ToolError):
    pass


class MalformedToolResponse(ToolError):
    pass


class InvalidRequest(ToolError):
    """The provider rejected the arguments (bad slot, past booking, ...)."""


class PermissionDenied(ToolError):
    """The authenticated caller may not act on this resource."""


class IdempotencyConflict(ToolError):
    """An idempotency key was reused with different arguments."""


# --- agent-level ---


class ApprovalInvalid(ErrandError):
    """No valid, matching, unconsumed grant exists for the requested side effect."""


class IllegalTransition(ErrandError):
    pass


class LimitExceeded(ErrandError):
    def __init__(self, limit: str, value: float) -> None:
        super().__init__(f"limit exceeded: {limit}={value}")
        self.limit = limit
        self.value = value
