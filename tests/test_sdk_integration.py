"""The SDK against the real gateway app (auth, RBAC, audit, SSE, response_format)."""
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "src"))

from gateway_client import AuthError, Client, PermissionDenied, RetryPolicy  # noqa: E402
from tests.test_api import Env  # noqa: E402


class City(BaseModel):
    city: str


@pytest.fixture()
def env():
    e = Env()
    yield e
    e.client.close()


def sdk(env, token):
    return Client("http://testserver", token, http_client=env.client, retry=RetryPolicy(max_retries=0))


def say(env, text):
    async def chat(model, messages, options):
        return {"message": {"content": text}, "prompt_eval_count": 4, "eval_count": 2}
    env.ollama.chat = chat


def test_chat_miss_then_hit_and_models(env):
    c = sdk(env, env.key(["viewer"]))
    assert c.chat("What is 2 + 2?").cache == "MISS"
    assert c.chat("What is 2 + 2?").cache_hit
    ids = c.models()
    assert ids and "large-1" not in ids


def test_stream_and_structured_through_the_real_gateway(env):
    c = sdk(env, env.key(["viewer"]))
    say(env, "Paris is lovely")
    stream = c.chat("Tell me", stream=True)
    assert "".join(stream) == "Paris is lovely" and stream.usage["total_tokens"] == 6
    say(env, '{"city": "Paris"}')
    assert c.chat("Where?", response_model=City).parsed == City(city="Paris")


def test_typed_errors_from_real_gateway(env):
    with pytest.raises(AuthError):
        sdk(env, "gk_bad.token").chat("hi")
    with pytest.raises(PermissionDenied) as exc:
        sdk(env, env.key(["viewer"])).chat("hi", model="large-1")
    assert exc.value.request_id  # taken from the gateway's x-request-id header


def test_sdk_request_id_reaches_the_audit_log(env):
    sdk(env, env.key(["viewer"])).chat("hi")
    import json
    meta = json.loads(env.audit_rows()[-1].details)["inference"]
    assert meta["request_id"]
