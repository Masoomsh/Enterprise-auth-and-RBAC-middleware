"""Internal Python SDK for the enterprise AI gateway."""
from ._common import ChatResult
from .client import AsyncChatStream, AsyncClient, ChatStream, Client
from .errors import (
    AuthError,
    GatewayError,
    GatewayUnavailable,
    InvalidRequest,
    PermissionDenied,
    RateLimited,
    StructuredOutputError,
)
from .evalmode import CaseResult, EvalCase, EvalReport, load_cases, run_eval
from .local import LocalGateway, ollama_responder
from .retry import RetryPolicy

__version__ = "0.1.0"

__all__ = [
    "AsyncChatStream", "AsyncClient", "AuthError", "CaseResult", "ChatResult", "ChatStream", "Client",
    "EvalCase", "EvalReport", "GatewayError", "GatewayUnavailable", "InvalidRequest", "LocalGateway",
    "PermissionDenied", "RateLimited", "RetryPolicy", "StructuredOutputError", "load_cases",
    "ollama_responder", "run_eval",
]
