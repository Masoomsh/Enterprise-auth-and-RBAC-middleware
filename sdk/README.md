# gateway-client

Internal Python SDK for the enterprise AI gateway. Retries, streaming, structured output, and a
local mode for evals. Python 3.10+.

```bash
pip install gateway-client            # from the internal index
pip install "gateway-client[yaml]"    # also read YAML eval files
```

## Quickstart

```python
from gateway_client import Client

client = Client("https://gateway.internal", api_key="gk_...")   # or set GATEWAY_URL / GATEWAY_API_KEY
reply = client.chat("Explain semantic caching in one sentence")
print(reply.content, reply.model, reply.cache)                  # cache is HIT, MISS or BYPASS
```

The key comes from the gateway's `/v1/api-keys`; its roles and scopes decide which models, tools and
data sources you may use. A forbidden call raises `PermissionDenied`. Keys are never printed or logged.

## Features

**Auto-retry.** Connect errors, timeouts, 429, 502, 503 and 504 are retried with exponential
backoff and jitter, honouring `Retry-After`. 400, 401, 403 and 422 are never retried. The same
`x-request-id` is sent on every attempt, so retries show up as one request in the audit log.

```python
from gateway_client import RetryPolicy
Client(..., retry=RetryPolicy(max_retries=5, backoff=1.0))
```

**Streaming.**

```python
with client.chat("Write a haiku", stream=True) as stream:
    for piece in stream:
        print(piece, end="", flush=True)
print(stream.cache, stream.usage)
```

Retries happen only before the first chunk. A stream cut mid-way raises `GatewayUnavailable`.
`AsyncClient` has the same API (`async for piece in await client.chat(..., stream=True)`).

**Structured output.**

```python
from pydantic import BaseModel

class City(BaseModel):
    city: str
    population: int

result = client.chat("Largest city in France?", response_model=City)
result.parsed            # City(city='Paris', population=...)
```

The schema goes to the gateway, which validates the reply. If the reply still does not parse, the
SDK retries once with a corrective message, then raises `StructuredOutputError` (with `.raw`).
Use `response_format={"type": "json_object"}` for untyped JSON.

**Local eval mode.** No gateway, no key, no network. `mode="local"` runs the full SDK path against
an in-process stand-in that mimics the gateway (cache, SSE, schema validation).

```python
from gateway_client import Client, load_cases, run_eval

with Client(mode="local") as c:                       # echo responder; or LocalGateway(ollama_responder("qwen2.5:3b"))
    report = run_eval(c, load_cases("cases.yaml"))
print(report.summary())
assert report.ok
```

```yaml
# cases.yaml
- name: capital
  prompt: What is the capital of France?
  expect: {contains: [Paris], max_latency_s: 5}
```

Checks: `contains`, `not_contains`, `equals`, `regex`, `json_schema`, `max_latency_s`, `cache`.

## Command line

Installing the package adds the `gw` command.

```bash
gw login                                   # asks for URL and key (hidden); verifies; saves ~/.gateway/config.json
gw models                                  # models your key may use
gw chat "Explain RBAC" --stream            # stream the answer; cache/model line goes to stderr
gw chat "Largest city?" --json-schema city.json
echo "long prompt" | gw chat - --model qwen2.5:3b
gw eval cases.yaml                         # exit code 1 if any case fails: use it in CI
gw eval cases.yaml --local                 # no gateway, no key
gw chat "hi" --local --ollama qwen2.5:3b   # real local Ollama model, no gateway
gw logout
```

Settings come from flags, then `GATEWAY_URL` / `GATEWAY_API_KEY`, then the saved login.
Exit codes: 0 ok, 1 eval failure, 2 bad usage or config, 3 gateway or network error.
Prefer the prompt or the environment variable over `--key`, since flags land in shell history.

## Errors

All derive from `GatewayError` and carry `.status_code` and `.request_id` (quote it to support).
`AuthError` (401), `PermissionDenied` (403), `InvalidRequest` (400/422), `RateLimited` (429),
`GatewayUnavailable` (network/5xx), `StructuredOutputError`.

## Development

```bash
cd sdk && pip install -e ".[dev]" && pytest
```
