"""Synchronous and asynchronous gateway clients."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Literal, overload

import httpx
from pydantic import BaseModel

from ._common import (
    ChatResult,
    build_payload,
    corrective_messages,
    delta_from_event,
    error_from_response,
    new_request_id,
    normalize_messages,
    parse_structured,
    resolve_format,
    result_from_response,
    sse_data,
)
from .errors import GatewayUnavailable, InvalidRequest, StructuredOutputError
from .retry import RetryPolicy, parse_retry_after

_NETWORK_ERRORS = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError)
_FORMAT_NUDGE = (
    "Your previous reply did not match the required format. Reply again with only valid JSON."
)


class _Config:
    def __init__(self, base_url, api_key, retry, timeout, mode, local):
        if mode not in ("remote", "local"):
            raise ValueError("mode must be 'remote' or 'local'")
        self.mode = mode
        if mode == "local":
            from .local import LocalGateway

            self.local = local or LocalGateway()
            base_url = "http://local.gateway"
        else:
            self.local = None
            base_url = base_url or os.getenv("GATEWAY_URL")
            api_key = api_key or os.getenv("GATEWAY_API_KEY")
            if not base_url:
                raise ValueError("base_url is required (or set GATEWAY_URL)")
            if not api_key:
                raise ValueError("api_key is required (or set GATEWAY_API_KEY)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.retry = retry or RetryPolicy()
        self.timeout = timeout

    def headers(self, request_id: str | None = None, bypass_cache: bool = False) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        if request_id:
            h["x-request-id"] = request_id
        if bypass_cache:
            h["x-cache-bypass"] = "true"
        return h

    def __repr__(self) -> str:  # never show the key
        return f"<gateway_client {self.mode} {self.base_url}>"


def _prepare(prompt, system, model, stream, temperature, top_p, max_tokens, tools, data_sources,
             response_model, response_format):
    if stream and (response_model is not None or response_format is not None):
        raise ValueError("stream=True cannot be combined with structured output")
    rf = resolve_format(response_model, response_format)
    messages = normalize_messages(prompt, system)
    kw = dict(model=model, stream=stream, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
              tools=tools, data_sources=data_sources, response_format=rf)
    return messages, kw


# ------------------------------------------------------------------ sync


class ChatStream:
    """Iterate to receive text chunks. Metadata is available as soon as the stream opens;
    `usage` fills in when the final chunk arrives. Retries happen only before the first chunk."""

    def __init__(self, response: httpx.Response, request_id: str):
        self._response = response
        self.cache = response.headers.get("x-cache", "UNKNOWN")
        self.model = response.headers.get("x-routed-model", "")
        self.request_id = response.headers.get("x-request-id") or request_id
        self.usage: dict[str, int] = {}
        self.text = ""

    def __iter__(self) -> Iterator[str]:
        try:
            for line in self._response.iter_lines():
                data = sse_data(line)
                if data is None:
                    continue
                if data == "[DONE]":
                    return
                piece, usage = delta_from_event(json.loads(data))
                if usage:
                    self.usage = usage
                if piece:
                    self.text += piece
                    yield piece
        except _NETWORK_ERRORS as exc:
            raise GatewayUnavailable("Stream interrupted", request_id=self.request_id) from exc
        finally:
            self.close()

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> ChatStream:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Client:
    def __init__(
        self, base_url: str | None = None, api_key: str | None = None, *, retry: RetryPolicy | None = None,
        timeout: float = 60.0, mode: str = "remote", local: Any = None,
        http_client: httpx.Client | None = None,
    ):
        self._cfg = _Config(base_url, api_key, retry, timeout, mode, local)
        if http_client is not None:
            self._http, self._owns = http_client, False
        else:
            transport = httpx.MockTransport(self._cfg.local.handle) if self._cfg.local else None
            self._http = httpx.Client(base_url=self._cfg.base_url, timeout=timeout, transport=transport)
            self._owns = True

    def __repr__(self) -> str:
        return repr(self._cfg)

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send(self, method: str, path: str, *, headers, json_body=None, stream=False) -> httpx.Response:
        policy = self._cfg.retry
        for attempt in range(policy.max_retries + 1):
            last = attempt == policy.max_retries
            try:
                request = self._http.build_request(method, path, headers=headers, json=json_body)
                response = self._http.send(request, stream=stream)
            except _NETWORK_ERRORS as exc:
                if last:
                    raise GatewayUnavailable(f"Could not reach gateway: {type(exc).__name__}",
                                             request_id=headers.get("x-request-id")) from exc
                policy.sleep(policy.delay(attempt))
                continue
            if response.status_code in policy.retry_statuses and not last:
                wait = policy.delay(attempt, parse_retry_after(response.headers.get("retry-after")))
                response.close()
                policy.sleep(wait)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            response.read()
            body = response.content
            response.close()
            raise error_from_response(response.status_code, response.headers, body)

    @overload
    def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: Literal[True], temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatStream: ...

    @overload
    def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: Literal[False] = False, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult: ...

    @overload
    def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: bool, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult | ChatStream: ...

    def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: bool = False, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult | ChatStream:
        messages, kw = _prepare(prompt, system, model, stream, temperature, top_p, max_tokens, tools,
                                data_sources, response_model, response_format)
        request_id = new_request_id()
        headers = self._cfg.headers(request_id, bypass_cache)
        if stream:
            response = self._send("POST", "/v1/chat/completions", headers=headers,
                                  json_body=build_payload(messages, **kw), stream=True)
            self._raise_for_status(response)
            return ChatStream(response, request_id)

        structured = kw["response_format"] is not None
        for attempt in range(2 if structured else 1):
            try:
                response = self._send("POST", "/v1/chat/completions", headers=headers,
                                      json_body=build_payload(messages, **kw))
                self._raise_for_status(response)
            except InvalidRequest as exc:
                if structured and exc.output_mismatch and attempt == 0:
                    messages = messages + [{"role": "user", "content": _FORMAT_NUDGE}]
                    continue
                if structured and exc.output_mismatch:
                    raise StructuredOutputError(
                        exc.message, status_code=exc.status_code, request_id=exc.request_id
                    ) from exc
                raise
            result = result_from_response(response.json(), response.headers, request_id)
            if not structured:
                return result
            try:
                result.parsed = parse_structured(result.content, response_model, request_id)
                return result
            except StructuredOutputError as exc:
                if attempt == 1:
                    raise
                messages = messages + corrective_messages(result.content, str(exc.message))
        raise AssertionError("unreachable")  # pragma: no cover

    def models(self) -> list[str]:
        response = self._send("GET", "/v1/models", headers=self._cfg.headers())
        self._raise_for_status(response)
        return [m["id"] for m in response.json().get("data", [])]

    def feedback(self, request_id: str, score: float) -> None:
        response = self._send("POST", "/v1/feedback", headers=self._cfg.headers(),
                              json_body={"request_id": request_id, "quality_score": score})
        self._raise_for_status(response)


# ------------------------------------------------------------------ async


class AsyncChatStream:
    def __init__(self, response: httpx.Response, request_id: str):
        self._response = response
        self.cache = response.headers.get("x-cache", "UNKNOWN")
        self.model = response.headers.get("x-routed-model", "")
        self.request_id = response.headers.get("x-request-id") or request_id
        self.usage: dict[str, int] = {}
        self.text = ""

    async def __aiter__(self) -> AsyncIterator[str]:
        try:
            async for line in self._response.aiter_lines():
                data = sse_data(line)
                if data is None:
                    continue
                if data == "[DONE]":
                    return
                piece, usage = delta_from_event(json.loads(data))
                if usage:
                    self.usage = usage
                if piece:
                    self.text += piece
                    yield piece
        except _NETWORK_ERRORS as exc:
            raise GatewayUnavailable("Stream interrupted", request_id=self.request_id) from exc
        finally:
            await self.close()

    async def close(self) -> None:
        await self._response.aclose()

    async def __aenter__(self) -> AsyncChatStream:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class AsyncClient:
    def __init__(
        self, base_url: str | None = None, api_key: str | None = None, *, retry: RetryPolicy | None = None,
        timeout: float = 60.0, mode: str = "remote", local: Any = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._cfg = _Config(base_url, api_key, retry, timeout, mode, local)
        if http_client is not None:
            self._http, self._owns = http_client, False
        else:
            transport = httpx.MockTransport(self._cfg.local.handle) if self._cfg.local else None
            self._http = httpx.AsyncClient(base_url=self._cfg.base_url, timeout=timeout, transport=transport)
            self._owns = True

    def __repr__(self) -> str:
        return repr(self._cfg)

    async def aclose(self) -> None:
        if self._owns:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _send(self, method: str, path: str, *, headers, json_body=None, stream=False) -> httpx.Response:
        policy = self._cfg.retry
        for attempt in range(policy.max_retries + 1):
            last = attempt == policy.max_retries
            try:
                request = self._http.build_request(method, path, headers=headers, json=json_body)
                response = await self._http.send(request, stream=stream)
            except _NETWORK_ERRORS as exc:
                if last:
                    raise GatewayUnavailable(f"Could not reach gateway: {type(exc).__name__}",
                                             request_id=headers.get("x-request-id")) from exc
                await policy.async_sleep(policy.delay(attempt))
                continue
            if response.status_code in policy.retry_statuses and not last:
                wait = policy.delay(attempt, parse_retry_after(response.headers.get("retry-after")))
                await response.aclose()
                await policy.async_sleep(wait)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            await response.aread()
            body = response.content
            await response.aclose()
            raise error_from_response(response.status_code, response.headers, body)

    @overload
    async def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: Literal[True], temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> AsyncChatStream: ...

    @overload
    async def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: Literal[False] = False, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult: ...

    @overload
    async def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: bool, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult | AsyncChatStream: ...

    async def chat(
        self, prompt: str | list[dict[str, str]], *, system: str | None = None, model: str | None = None,
        stream: bool = False, temperature: float | None = None, top_p: float | None = None,
        max_tokens: int | None = None, tools: Sequence[str] | None = None,
        data_sources: Sequence[str] | None = None, response_model: type[BaseModel] | None = None,
        response_format: dict[str, Any] | None = None, bypass_cache: bool = False,
    ) -> ChatResult | AsyncChatStream:
        messages, kw = _prepare(prompt, system, model, stream, temperature, top_p, max_tokens, tools,
                                data_sources, response_model, response_format)
        request_id = new_request_id()
        headers = self._cfg.headers(request_id, bypass_cache)
        if stream:
            response = await self._send("POST", "/v1/chat/completions", headers=headers,
                                        json_body=build_payload(messages, **kw), stream=True)
            await self._raise_for_status(response)
            return AsyncChatStream(response, request_id)

        structured = kw["response_format"] is not None
        for attempt in range(2 if structured else 1):
            try:
                response = await self._send("POST", "/v1/chat/completions", headers=headers,
                                            json_body=build_payload(messages, **kw))
                await self._raise_for_status(response)
            except InvalidRequest as exc:
                if structured and exc.output_mismatch and attempt == 0:
                    messages = messages + [{"role": "user", "content": _FORMAT_NUDGE}]
                    continue
                if structured and exc.output_mismatch:
                    raise StructuredOutputError(
                        exc.message, status_code=exc.status_code, request_id=exc.request_id
                    ) from exc
                raise
            result = result_from_response(response.json(), response.headers, request_id)
            if not structured:
                return result
            try:
                result.parsed = parse_structured(result.content, response_model, request_id)
                return result
            except StructuredOutputError as exc:
                if attempt == 1:
                    raise
                messages = messages + corrective_messages(result.content, str(exc.message))
        raise AssertionError("unreachable")  # pragma: no cover

    async def models(self) -> list[str]:
        response = await self._send("GET", "/v1/models", headers=self._cfg.headers())
        await self._raise_for_status(response)
        return [m["id"] for m in response.json().get("data", [])]

    async def feedback(self, request_id: str, score: float) -> None:
        response = await self._send("POST", "/v1/feedback", headers=self._cfg.headers(),
                                    json_body={"request_id": request_id, "quality_score": score})
        await self._raise_for_status(response)
