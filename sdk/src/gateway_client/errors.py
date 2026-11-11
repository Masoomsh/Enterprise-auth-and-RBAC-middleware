"""Typed errors. Every error carries the gateway's x-request-id so it can be quoted to support."""
from __future__ import annotations


class GatewayError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id

    def __str__(self) -> str:
        suffix = f" (request id {self.request_id})" if self.request_id else ""
        return f"{self.message}{suffix}"


class AuthError(GatewayError):
    """401: missing, invalid or expired credential."""


class PermissionDenied(GatewayError):
    """403: the identity's role or key scope does not allow this model, tool, data source or route."""


class InvalidRequest(GatewayError):
    """400/422: the gateway rejected the request body."""

    @property
    def output_mismatch(self) -> bool:
        return "response_format" in self.message


class RateLimited(GatewayError):
    """429 after all retries."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class GatewayUnavailable(GatewayError):
    """Network failure, timeout, or 5xx after all retries."""


class StructuredOutputError(GatewayError):
    """The model's reply could not be parsed into the requested structure."""

    def __init__(self, message: str, *, raw: str = "", **kw):
        super().__init__(message, **kw)
        self.raw = raw
