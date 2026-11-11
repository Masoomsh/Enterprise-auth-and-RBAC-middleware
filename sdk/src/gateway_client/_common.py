"""Pure helpers shared by the sync and async clients."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .errors import (
    AuthError,
    GatewayError,
    GatewayUnavailable,
    InvalidRequest,
    PermissionDenied,
    RateLimited,
    StructuredOutputError,
)
from .retry import parse_retry_after


@dataclass
class ChatResult:
    content: str
    model: str
    cache: str  # HIT | MISS | BYPASS | UNKNOWN
    request_id: str
    usage: dict[str, int] = field(default_factory=dict)
    parsed: Any = None  # a response_model instance, or the decoded JSON for response_format
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        return self.cache == "HIT"


def normalize_messages(prompt: str | list[dict[str, str]], system: str | None) -> list[dict[str, str]]:
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else [dict(m) for m in prompt]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


def resolve_format(
    response_model: type[BaseModel] | None, response_format: dict[str, Any] | None
) -> dict[str, Any] | None:
    if response_model is not None and response_format is not None:
        raise ValueError("Pass response_model or response_format, not both")
    if response_model is not None:
        return {
            "type": "json_schema",
            "json_schema": {"name": response_model.__name__, "schema": response_model.model_json_schema()},
        }
    return response_format


def build_payload(
    messages: list[dict[str, str]], *, model, stream, temperature, top_p, max_tokens, tools,
    data_sources, response_format,
) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": messages, "stream": stream}
    for key, value in (
        ("model", model), ("temperature", temperature), ("top_p", top_p), ("max_tokens", max_tokens),
        ("response_format", response_format),
    ):
        if value is not None:
            body[key] = value
    if tools:
        body["tools"] = list(tools)
    if data_sources:
        body["data_sources"] = list(data_sources)
    return body


def new_request_id() -> str:
    return str(uuid.uuid4())


def error_from_response(status: int, headers: Any, body: bytes) -> GatewayError:
    request_id = headers.get("x-request-id")
    try:
        detail = json.loads(body).get("detail")
        message = detail if isinstance(detail, str) else json.dumps(detail)
    except (ValueError, AttributeError):
        message = body.decode("utf-8", "replace")[:200] or f"HTTP {status}"
    kw = {"status_code": status, "request_id": request_id}
    if status == 401:
        return AuthError(message, **kw)
    if status == 403:
        return PermissionDenied(message, **kw)
    if status in (400, 422):
        return InvalidRequest(message, **kw)
    if status == 429:
        return RateLimited(message, retry_after=parse_retry_after(headers.get("retry-after")), **kw)
    if status >= 500:
        return GatewayUnavailable(message, **kw)
    return GatewayError(message, **kw)


def result_from_response(data: dict[str, Any], headers: Any, request_id: str) -> ChatResult:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GatewayError("Unexpected response shape from gateway", request_id=request_id) from exc
    return ChatResult(
        content=content,
        model=headers.get("x-routed-model") or data.get("model", ""),
        cache=headers.get("x-cache", "UNKNOWN"),
        request_id=headers.get("x-request-id") or data.get("id") or request_id,
        usage=data.get("usage") or {},
        raw=data,
    )


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_structured(content: str, response_model: type[BaseModel] | None, request_id: str) -> Any:
    text = _FENCE.sub("", content.strip()).strip()
    try:
        if response_model is not None:
            return response_model.model_validate_json(text)
        return json.loads(text)
    except (ValueError, ValidationError) as exc:
        raise StructuredOutputError(
            f"Reply is not valid {response_model.__name__ if response_model else 'JSON'}: {exc}",
            raw=content, request_id=request_id,
        ) from exc


def corrective_messages(content: str, error: str) -> list[dict[str, str]]:
    return [
        {"role": "assistant", "content": content},
        {"role": "user", "content": f"That reply was invalid ({error[:300]}). Reply again with only valid JSON."},
    ]


def sse_data(line: str) -> str | None:
    return line[6:] if line.startswith("data: ") else None


def delta_from_event(event: dict[str, Any]) -> tuple[str, dict[str, int] | None]:
    choice = (event.get("choices") or [{}])[0]
    return (choice.get("delta") or {}).get("content", ""), event.get("usage")
