# 15-minute demo: gateway-client (SDK + `gw` CLI)

Audience: engineers on teams that call, or will call, the AI gateway. Goal: they leave knowing how to
install it, what it saves them, and what to do on Monday. Record it once, in one take, at 1080p.

## Before you start (10 minutes, off camera)

- Backend running (`uvicorn app.main:app --port 8000`), Ollama running, `gw logout` done.
- An API key ready in a text file (do not show it on screen). Create it at http://localhost:8000/docs
  after signing in at http://localhost:3000 (`POST /v1/api-keys`, role `engineer`, scope `*`).
- A clean terminal with a large font, and a fresh virtualenv: `python -m venv demo && demo\Scripts\activate`.
- Open in tabs: the SDK README, http://localhost:3000/app, http://localhost:8000/dashboard/ui.
- Do a dry run. Restart the backend before recording so the first call is a cache MISS.

## Run of show

| Min | Segment | What you say and do |
|---|---|---|
| 0:00 | The problem (1) | "Every team writes its own retry loop, its own SSE parser, its own JSON-fixing code, and tests prompts by hand against a shared gateway." Show a 15-line `requests` snippet with a retry loop. |
| 1:00 | Install (1) | `pip install gateway-client` (wheel from the internal index). `gw --version`. |
| 2:00 | Login (1) | `gw login --url http://localhost:8000`, paste the key (hidden). Point out: verified against the gateway, saved owner-only, never echoed. |
| 3:00 | Chat and cache (2) | `gw chat "What is semantic caching?"` then run it again. Show `cache=MISS` then `cache=HIT` and the request id. Switch to http://localhost:3000/app: the stats moved. |
| 5:00 | Streaming (1) | `gw chat "Write a haiku about caching" --stream`. Then the 5-line Python version with `with client.chat(..., stream=True)`. |
| 6:00 | Structured output (2) | Show `Ticket(BaseModel)` and `client.chat(..., response_model=Ticket)`. Run `examples/quickstart.py --remote`. Say: "the gateway validates the schema and the SDK retries once if the model misses." |
| 8:00 | Retries (1.5) | Stop the backend mid-demo, run a chat in one terminal, restart the backend, show the call succeeding. Then show a 403: `gw chat "hi" --model not-allowed` gives `PermissionDenied` with a request id, and is not retried. |
| 9:30 | Local eval mode (2.5) | Show `examples/cases.yaml`. `gw eval examples/cases.yaml --local` runs with no gateway and no key. Show a failing case and `echo $LASTEXITCODE` = 1. Show the CI line: `run: gw eval cases.yaml --local`. Optional: `--ollama qwen2.5:3b` for a real model. |
| 12:00 | Security and audit (1) | RBAC decides the models; the SDK cannot bypass it. Every call (including retries, one request id) is in the audit log. Show http://localhost:8000/dashboard/ui. |
| 13:00 | Adopt it (1) | Show the migration guide: three changes, one afternoon. Name the two pilot teams and the one engineer on each. |
| 14:00 | Q&A (1) | Bring up the README. Ask for their first feature request. |

## Things that go wrong on camera

- First call slow: Ollama is loading the model. Warm it with one chat before recording.
- `PermissionDenied` on a model you expected to work: the key's role or scope is too narrow. Use `engineer` and `*`.
- 401 right after login: the key was copied with a trailing space, or it has expired (90-day default).
- Never leave the key, `~/.gateway/config.json` or a browser cookie visible in the recording.

## After the recording

Upload the video where both teams can reach it, link it from the README, and post the repository
link, the install command and the migration guide in each team's channel the same day.
