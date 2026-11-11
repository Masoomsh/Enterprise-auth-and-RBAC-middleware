"""Streaming (SSE), structured output (response_format) and /v1/models on the gateway."""
import json

import pytest

from tests.test_api import Env, bearer

SCHEMA = {
    "type": "json_schema",
    "json_schema": {"schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }},
}


@pytest.fixture()
def env():
    e = Env()
    yield e
    e.client.close()


def reply(env, content):
    async def chat(model, messages, options):
        env.ollama.chat_calls.append(messages)
        return {"message": {"content": content}, "prompt_eval_count": 5, "eval_count": 2}
    env.ollama.chat = chat


def events(response):
    lines = [l for l in response.text.split("\n\n") if l.startswith("data: ")]
    return [l[6:] for l in lines]


def test_stream_returns_sse_chunks_then_done(env):
    reply(env, "Paris is the capital")
    r = env.chat(env.key(["viewer"]), stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-cache"] == "MISS" and r.headers["x-routed-model"]
    ev = events(r)
    assert ev[-1] == "[DONE]"
    chunks = [json.loads(e) for e in ev[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Paris is the capital"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def test_stream_is_authenticated_authorized_and_audited(env):
    assert env.chat(None, stream=True).status_code == 401
    token = env.key(["viewer"])
    assert env.chat(token, stream=True, model="large-1").status_code == 403
    assert env.chat(token, stream=True).status_code == 200
    row = env.audit_rows()[-1]
    assert row.decision == "allow" and json.loads(row.details)["inference"]["stream"] is True


def test_stream_hit_uses_cache(env):
    reply(env, "cached text")
    token = env.key(["viewer"])
    env.chat(token)
    r = env.chat(token, stream=True)
    assert r.headers["x-cache"] == "HIT" and len(env.ollama.chat_calls) == 1


def test_structured_output_valid_json_is_returned_and_cached(env):
    reply(env, '{"city": "Paris"}')
    token = env.key(["viewer"])
    first = env.chat(token, response_format=SCHEMA)
    assert first.status_code == 200
    assert json.loads(first.json()["choices"][0]["message"]["content"]) == {"city": "Paris"}
    # The schema instruction was sent to the model as a system message.
    assert env.ollama.chat_calls[0][0]["role"] == "system"
    assert env.chat(token, response_format=SCHEMA).headers["x-cache"] == "HIT"


def test_structured_output_tolerates_code_fence(env):
    reply(env, '```json\n{"city": "Paris"}\n```')
    assert env.chat(env.key(["viewer"]), response_format=SCHEMA).status_code == 200


@pytest.mark.parametrize("content", ["not json", '{"town": "Paris"}', '["a"]'])
def test_structured_output_mismatch_is_422_and_not_cached(env, content):
    reply(env, content)
    token = env.key(["viewer"])
    assert env.chat(token, response_format=SCHEMA).status_code == 422
    reply(env, '{"city": "Paris"}')
    r = env.chat(token, response_format=SCHEMA)
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS"  # bad answer was not stored


def test_response_format_scopes_the_cache(env):
    reply(env, '{"city": "Paris"}')
    token = env.key(["viewer"])
    env.chat(token)  # plain call
    assert env.chat(token, response_format=SCHEMA).headers["x-cache"] == "MISS"


@pytest.mark.parametrize(
    "fmt",
    [{"type": "xml"}, {"type": "json_schema"}, {"type": "json_schema", "json_schema": {"schema": {"type": 5}}}],
)
def test_bad_response_format_is_422(env, fmt):
    assert env.chat(env.key(["viewer"]), response_format=fmt).status_code == 422
    assert env.ollama.chat_calls == []


def test_models_requires_auth_and_lists_only_permitted(env):
    assert env.client.get("/v1/models").status_code == 401
    viewer = env.client.get("/v1/models", headers=bearer(env.key(["viewer"], owner="a")))
    engineer = env.client.get("/v1/models", headers=bearer(env.key(["engineer"], owner="b")))
    v = {m["id"] for m in viewer.json()["data"]}
    e = {m["id"] for m in engineer.json()["data"]}
    assert viewer.status_code == 200 and "large-1" not in v and "large-1" in e
