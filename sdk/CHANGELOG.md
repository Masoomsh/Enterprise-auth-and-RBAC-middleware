# Changelog

All notable changes to `gateway-client` are recorded here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - unreleased

### Added
- `Client` and `AsyncClient` for chat, models and feedback.
- Auto-retry with exponential backoff, jitter and `Retry-After` support (`RetryPolicy`).
- Streaming chat over server-sent events.
- Structured output via `response_model` (pydantic) or `response_format`, with one corrective retry.
- Local mode (`mode="local"`) and an eval runner (`load_cases`, `run_eval`).
- `gw` command line: `login`, `logout`, `models`, `chat` and `eval`, with a local mode.
- Typed errors that carry the gateway request id.
