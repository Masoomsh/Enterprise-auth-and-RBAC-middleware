# Adopting gateway-client

For a team that calls the gateway today with `requests`, `httpx` or `curl`.

## 1. Get a key

Ask the gateway owners for an API key with the narrowest role and scopes your service needs. Keys expire
after 90 days. Store it in your secret manager and expose it as `GATEWAY_API_KEY`; also set `GATEWAY_URL`.

## 2. Swap the calls (one afternoon)

Before:

```python
for attempt in range(3):
    r = requests.post(f"{URL}/v1/chat/completions", json={"messages": [...]}, headers={"Authorization": f"Bearer {KEY}"})
    if r.status_code in (502, 503):
        time.sleep(2 ** attempt)
        continue
    break
text = r.json()["choices"][0]["message"]["content"]
data = json.loads(text)        # hope the model returned JSON
```

After:

```python
from gateway_client import Client
client = Client()              # reads GATEWAY_URL and GATEWAY_API_KEY
data = client.chat("...", response_model=MyModel).parsed
```

You get retries with backoff, the request id on every error, typed exceptions, and schema-checked output.

## 3. Move prompt tests into CI

Write `cases.yaml` for your five most important prompts and run `gw eval cases.yaml --local` on every
pull request (no gateway or key needed), and `gw eval cases.yaml` nightly against the real gateway.

## Checklist for a pilot team

- [ ] Key issued with minimal role and scopes
- [ ] SDK installed from the internal index, version pinned (`gateway-client==0.1.*`)
- [ ] One production code path migrated, old retry code deleted
- [ ] `gw eval` running in CI
- [ ] Feedback sent to the SDK owners after one week (what was awkward, what was missing)

## Support

Quote the request id from any error. The gateway owners can find the call in the audit log with it.
