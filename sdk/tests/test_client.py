import json

import httpx
import pytest
from pydantic import BaseModel

from gateway_client import (
    AsyncClient,
    AuthError,
    Client,
    GatewayUnavailable,
    InvalidRequest,
    PermissionDenied,
    RateLimited,
    RetryPolicy,
    StructuredOutputError,
)


def ok(content="hi", **headers):
    return httpx.Response(
        200,
        headers={"x-cache": "MISS", "x-routed-model": "m1", "x-request-id": "rid", **headers},
        json={"id": "rid", "model": "m1", "choices": [{"message": {"content": content}}],
              "usage": {"total_tokens": 3}},
    )


def make(handler, **policy):
    sleeps = []
    retry = RetryPolicy(sleep=sleeps.append, jitter=False, **policy)
    http = httpx.Client(base_url="http://gw", transport=httpx.MockTransport(handler))
    return Client("http://gw", "gk_secret", retry=retry, http_client=http), sleeps


def test_chat_sends_key_request_id_and_returns_metadata():
    seen = []

    def handler(req):
        seen.append(req)
        return ok("hello")

    client, _ = make(handler)
    r = client.chat("hi", system="be brief", model="m1", max_tokens=5, tools=["search"], bypass_cache=True)
    assert (r.content, r.model, r.cache, r.cache_hit) == ("hello", "m1", "MISS", False)
    req = seen[0]
    assert req.headers["authorization"] == "Bearer gk_secret" and req.headers["x-cache-bypass"] == "true"
    body = json.loads(req.content)
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["tools"] == ["search"] and body["max_tokens"] == 5 and body["stream"] is False


def test_key_never_appears_in_repr():
    client, _ = make(lambda r: ok())
    assert "gk_secret" not in repr(client)


def test_config_from_environment(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://x")
    monkeypatch.setenv("GATEWAY_API_KEY", "gk_1")
    Client().close()
    monkeypatch.delenv("GATEWAY_API_KEY")
    with pytest.raises(ValueError):
        Client()


# ---- retry -----------------------------------------------------------------------------

def test_retries_503_then_succeeds_with_exponential_backoff_and_same_request_id():
    calls = []

    def handler(req):
        calls.append(req.headers["x-request-id"])
        return httpx.Response(503) if len(calls) < 3 else ok()

    client, sleeps = make(handler)
    assert client.chat("hi").content == "hi"
    assert len(calls) == 3 and len(set(calls)) == 1
    assert sleeps == [0.5, 1.0]


def test_retry_after_header_is_honoured_and_capped():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "100"}) if len(calls) == 1 else ok()

    client, sleeps = make(handler, max_retry_after=7)
    client.chat("hi")
    assert sleeps == [7]


def test_gives_up_after_max_retries_with_typed_error():
    client, sleeps = make(lambda r: httpx.Response(429, json={"detail": "slow down"}), max_retries=2)
    with pytest.raises(RateLimited):
        client.chat("hi")
    assert len(sleeps) == 2

    client, _ = make(lambda r: httpx.Response(502), max_retries=1)
    with pytest.raises(GatewayUnavailable):
        client.chat("hi")


def test_network_errors_are_retried_then_raise_unavailable():
    def handler(req):
        raise httpx.ConnectError("down")

    client, sleeps = make(handler, max_retries=2)
    with pytest.raises(GatewayUnavailable):
        client.chat("hi")
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "status,error", [(401, AuthError), (403, PermissionDenied), (422, InvalidRequest), (400, InvalidRequest)]
)
def test_client_errors_are_never_retried(status, error):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"detail": "nope"}, headers={"x-request-id": "r9"})

    client, sleeps = make(handler)
    with pytest.raises(error) as exc:
        client.chat("hi")
    assert len(calls) == 1 and sleeps == [] and exc.value.request_id == "r9"
    assert "r9" in str(exc.value)


# ---- streaming -------------------------------------------------------------------------

def sse(*pieces, usage=None):
    def ev(d, finish=None, extra=None):
        return "data: " + json.dumps({"choices": [{"delta": d, "finish_reason": finish}], **(extra or {})}) + "\n\n"

    body = ev({"role": "assistant"}) + "".join(ev({"content": p}) for p in pieces)
    body += ev({}, "stop", {"usage": usage or {"total_tokens": 4}}) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"x-cache": "HIT", "x-routed-model": "m1"})


def test_stream_yields_pieces_and_exposes_metadata():
    client, _ = make(lambda r: sse("Hel", "lo ", "world"))
    with client.chat("hi", stream=True) as stream:
        assert (stream.cache, stream.model) == ("HIT", "m1")
        assert list(stream) == ["Hel", "lo ", "world"]
        assert stream.text == "Hello world" and stream.usage == {"total_tokens": 4}


def test_stream_retries_before_first_chunk_but_not_after():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else sse("ok")

    client, sleeps = make(handler)
    assert list(client.chat("hi", stream=True)) == ["ok"] and len(sleeps) == 1


def test_stream_errors_use_typed_exceptions():
    client, _ = make(lambda r: httpx.Response(403, json={"detail": "no"}))
    with pytest.raises(PermissionDenied):
        client.chat("hi", stream=True)


def test_stream_with_structured_output_is_rejected():
    client, _ = make(lambda r: ok())
    with pytest.raises(ValueError):
        client.chat("hi", stream=True, response_format={"type": "json_object"})


# ---- structured output -----------------------------------------------------------------

class City(BaseModel):
    city: str
    population: int


def test_response_model_is_parsed_and_schema_is_sent():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return ok('{"city": "Paris", "population": 2100000}')

    client, _ = make(handler)
    r = client.chat("hi", response_model=City)
    assert r.parsed == City(city="Paris", population=2100000)
    assert seen[0]["response_format"]["json_schema"]["schema"]["required"] == ["city", "population"]


def test_response_model_tolerates_code_fence():
    client, _ = make(lambda r: ok('```json\n{"city": "Rome", "population": 1}\n```'))
    assert client.chat("hi", response_model=City).parsed.city == "Rome"


def test_invalid_reply_gets_one_corrective_retry_then_succeeds():
    replies = iter(["not json", '{"city": "Oslo", "population": 5}'])
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["messages"])
        return ok(next(replies))

    client, _ = make(handler)
    assert client.chat("hi", response_model=City).parsed.city == "Oslo"
    assert [m["role"] for m in seen[1]] == ["user", "assistant", "user"]


def test_still_invalid_after_retry_raises_structured_output_error():
    client, _ = make(lambda r: ok("nope"))
    with pytest.raises(StructuredOutputError) as exc:
        client.chat("hi", response_model=City)
    assert exc.value.raw == "nope"


def test_gateway_422_output_mismatch_is_retried_once():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(422, json={"detail": "Model output did not match response_format"})
        return ok('{"a": 1}')

    client, _ = make(handler)
    r = client.chat("hi", response_format={"type": "json_object"})
    assert r.parsed == {"a": 1} and len(calls) == 2


# ---- other calls + async ---------------------------------------------------------------

def test_models_and_feedback():
    def handler(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
        return httpx.Response(204)

    client, _ = make(handler)
    assert client.models() == ["a", "b"]
    client.feedback("rid", 5)


async def test_async_client_retry_stream_and_structured():
    calls = []
    slept = []

    async def sleeper(s):
        slept.append(s)

    def handler(req):
        calls.append(1)
        body = json.loads(req.content) if req.content else {}
        if len(calls) == 1:
            return httpx.Response(503)
        if body.get("stream"):
            return sse("a", "b")
        return ok('{"city": "X", "population": 1}')

    http = httpx.AsyncClient(base_url="http://gw", transport=httpx.MockTransport(handler))
    async with AsyncClient("http://gw", "gk", http_client=http,
                           retry=RetryPolicy(async_sleep=sleeper, jitter=False)) as client:
        stream = await client.chat("hi", stream=True)
        assert [p async for p in stream] == ["a", "b"] and slept == [0.5]
        r = await client.chat("hi", response_model=City)
        assert r.parsed.city == "X"
