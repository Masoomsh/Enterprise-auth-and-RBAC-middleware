"""Local mode: an in-process stand-in for the gateway. No network, no key, no Pinecone.

`Client(mode="local")` routes every call through `LocalGateway.handle`, so the whole SDK code
path (retries, streaming, structured output) runs exactly as it does against the real gateway.
It mimics the gateway contract: x-cache HIT/MISS from an exact-match cache, x-routed-model,
SSE streaming, `response_format` validation (422 on a miss) and /v1/models.

Answers come from a `responder(messages, schema) -> str`. The default is a deterministic echo
(and a schema-shaped sample when a schema is requested); `ollama_responder` talks to a real
local Ollama so teams can evaluate prompts against a real model without the gateway.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from jsonschema import Draft202012Validator, ValidationError

Responder = Callable[[list[dict[str, str]], "dict[str, Any] | None"], str]


def sample_from_schema(schema: dict[str, Any]) -> Any:
    """Smallest value that satisfies a simple JSON schema (used by the default responder)."""
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "anyOf" in schema:
        return sample_from_schema(schema["anyOf"][0])
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: sample_from_schema(props[k]) for k in schema.get("required", props)}
    if kind == "array":
        return [sample_from_schema(schema.get("items", {}))] if schema.get("minItems") else []
    return {"string": "sample", "integer": 0, "number": 0.0, "boolean": True, "null": None}.get(kind or "", "sample")


def default_responder(messages: list[dict[str, str]], schema: dict[str, Any] | None) -> str:
    if schema is not None:
        return json.dumps(sample_from_schema(schema))
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return f"[local] {last}"


def ollama_responder(model: str, base_url: str = "http://127.0.0.1:11434", timeout: float = 120.0) -> Responder:
    def respond(messages: list[dict[str, str]], schema: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if schema is not None:
            body["format"] = schema
        reply = httpx.post(f"{base_url.rstrip('/')}/api/chat", json=body, timeout=timeout)
        reply.raise_for_status()
        return reply.json()["message"]["content"]

    return respond


def _schema_of(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    if not response_format:
        return None
    if response_format.get("type") == "json_object":
        return {"type": "object"}
    return (response_format.get("json_schema") or {}).get("schema")


class LocalGateway:
    def __init__(
        self, responder: Responder | None = None, models: list[str] | None = None, model: str = "local-model",
    ):
        self.responder = responder or default_responder
        self.model = model
        self.models = models or [model]
        self.cache: dict[str, tuple[str, int, int]] = {}
        self.calls = 0  # number of times the responder ran (cache misses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models" and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model"} for m in self.models]})
        if request.url.path == "/v1/feedback" and request.method == "POST":
            return httpx.Response(204)
        if request.url.path != "/v1/chat/completions" or request.method != "POST":
            return httpx.Response(404, json={"detail": "Not found"})
        return self._chat(request)

    def _chat(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        messages: list[dict[str, str]] = body.get("messages") or []
        if not messages:
            return httpx.Response(422, json={"detail": "messages must not be empty"})
        model = body.get("model") or self.model
        schema = _schema_of(body.get("response_format"))
        bypass = request.headers.get("x-cache-bypass", "").lower() in {"1", "true", "yes"}
        key = hashlib.sha256(json.dumps([messages, schema, model], sort_keys=True).encode()).hexdigest()

        cache_state = "BYPASS" if bypass else "MISS"
        if not bypass and key in self.cache:
            content, prompt_tokens, completion_tokens = self.cache[key]
            cache_state = "HIT"
        else:
            self.calls += 1
            content = self.responder(messages, schema)
            if schema is not None and not _valid(content, schema):
                return httpx.Response(
                    422, json={"detail": "Model output did not match response_format"},
                    headers={"x-request-id": request_id},
                )
            prompt_tokens = sum(len(m["content"].split()) for m in messages)
            completion_tokens = len(content.split())
            if not bypass:
                self.cache[key] = (content, prompt_tokens, completion_tokens)

        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        headers = {"x-cache": cache_state, "x-request-id": request_id, "x-routed-model": model}
        if body.get("stream"):
            return httpx.Response(200, content=_sse(request_id, model, content, usage).encode(),
                                  headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, headers=headers, json={
            "id": request_id, "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": usage,
        })


def _valid(content: str, schema: dict[str, Any]) -> bool:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        Draft202012Validator(schema).validate(json.loads(text))
    except (ValueError, ValidationError):
        return False
    return True


def _sse(request_id: str, model: str, content: str, usage: dict[str, int]) -> str:
    def event(delta: dict[str, Any], finish: str | None = None, extra: dict[str, Any] | None = None) -> str:
        chunk = {"id": request_id, "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **(extra or {})}
        return f"data: {json.dumps(chunk)}\n\n"

    out = [event({"role": "assistant"})]
    out += [event({"content": piece}) for piece in _split(content)]
    out += [event({}, "stop", {"usage": usage}), "data: [DONE]\n\n"]
    return "".join(out)


def _split(text: str) -> list[str]:
    import re

    return re.findall(r"\S+\s*|\s+", text)
