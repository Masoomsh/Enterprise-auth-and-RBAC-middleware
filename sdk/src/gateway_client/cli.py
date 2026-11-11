"""`gw` command line: chat, models, eval, login, logout.

Settings are resolved in this order: command-line flags, environment (GATEWAY_URL,
GATEWAY_API_KEY), then the file written by `gw login` (~/.gateway/config.json, owner-only).
Exit codes: 0 ok, 1 eval case failed, 2 bad usage/config, 3 gateway or network error.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .client import Client
from .errors import GatewayError, StructuredOutputError
from .evalmode import load_cases, run_eval
from .local import LocalGateway, ollama_responder


def config_path() -> Path:
    return Path(os.getenv("GATEWAY_CONFIG_DIR") or Path.home() / ".gateway") / "config.json"


def load_config() -> dict[str, str]:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(url: str, key: str) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions from the start; never write the key to a world-readable file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "api_key": key}, handle)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover  (limited chmod semantics on some filesystems)
        pass
    return path


class UsageError(Exception):
    pass


def _client_factory(**kwargs: Any) -> Client:  # indirection so tests can inject a transport
    return Client(**kwargs)


def build_client(args: argparse.Namespace) -> Client:
    if getattr(args, "local", False) or getattr(args, "ollama", None):
        gateway = LocalGateway(ollama_responder(args.ollama)) if args.ollama else LocalGateway()
        return _client_factory(mode="local", local=gateway)
    saved = load_config()
    url = args.url or os.getenv("GATEWAY_URL") or saved.get("url")
    key = os.getenv("GATEWAY_API_KEY") or saved.get("api_key")
    if not url or not key:
        raise UsageError("Not configured. Run `gw login`, or set GATEWAY_URL and GATEWAY_API_KEY, "
                         "or add --local to try the CLI without a gateway.")
    return _client_factory(base_url=url, api_key=key, timeout=args.timeout)


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


# ------------------------------------------------------------------ commands

def cmd_login(args: argparse.Namespace) -> int:
    url = (args.url or input("Gateway URL: ")).strip().rstrip("/")
    key = args.key or os.getenv("GATEWAY_API_KEY") or getpass.getpass("API key (hidden): ")
    if not url or not key:
        raise UsageError("Both a URL and an API key are required.")
    if not args.no_verify:
        with _client_factory(base_url=url, api_key=key, timeout=args.timeout) as client:
            models = client.models()  # 401 here means the key is wrong
        _err(f"Signed in. You may use {len(models)} model(s).")
    path = save_config(url, key)
    _err(f"Saved to {path}")
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    try:
        config_path().unlink()
        _err("Removed saved credentials.")
    except FileNotFoundError:
        _err("Nothing to remove.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    with build_client(args) as client:
        models = client.models()
    if args.json:
        _out(json.dumps(models))
    else:
        for name in models:
            _out(name)
    return 0


def _read_prompt(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def cmd_chat(args: argparse.Namespace) -> int:
    prompt = _read_prompt(args.prompt).strip()
    if not prompt:
        raise UsageError("Empty prompt.")
    schema = None
    if args.json_schema:
        try:
            schema = json.loads(Path(args.json_schema).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"Cannot read schema file: {exc}") from exc
        if args.stream:
            raise UsageError("--stream cannot be combined with --json-schema.")
    options: dict[str, Any] = dict(
        system=args.system, model=args.model, temperature=args.temperature, max_tokens=args.max_tokens,
        tools=args.tool or None, data_sources=args.data_source or None, bypass_cache=args.bypass_cache,
    )
    with build_client(args) as client:
        if args.stream:
            with client.chat(prompt, stream=True, **options) as stream:
                for piece in stream:
                    sys.stdout.write(piece)
                    sys.stdout.flush()
                _out()
            meta = {"cache": stream.cache, "model": stream.model, "request_id": stream.request_id,
                    "usage": stream.usage}
        else:
            if schema is not None:
                options["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
            result = client.chat(prompt, **options)
            if args.json:
                _out(json.dumps({"content": result.content, "model": result.model, "cache": result.cache,
                                 "request_id": result.request_id, "usage": result.usage}))
            elif schema is not None:
                _out(json.dumps(result.parsed, indent=2, ensure_ascii=False))
            else:
                _out(result.content)
            meta = {"cache": result.cache, "model": result.model, "request_id": result.request_id,
                    "usage": result.usage}
    if not args.quiet and not args.json:
        _err(f"[cache={meta['cache']} model={meta['model']} tokens={meta['usage'].get('total_tokens', '?')} "
             f"request_id={meta['request_id']}]")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.file)
    except (OSError, ValueError, RuntimeError) as exc:
        raise UsageError(str(exc)) from exc
    with build_client(args) as client:
        report = run_eval(client, cases, fail_fast=args.fail_fast)
    _out(json.dumps(report.to_dict(), indent=2) if args.json else report.summary())
    return 0 if report.ok else 1


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", help="Gateway base URL (default: GATEWAY_URL or saved login)")
    common.add_argument("--timeout", type=float, default=60.0, help="Request timeout in seconds")
    common.add_argument("--local", action="store_true", help="Use the in-process local gateway (no network)")
    common.add_argument("--ollama", metavar="MODEL", help="With local mode, answer from this Ollama model")
    common.add_argument("--json", action="store_true", help="Machine-readable output")

    parser = argparse.ArgumentParser(prog="gw", description="Command line for the enterprise AI gateway.")
    parser.add_argument("--version", action="version", version=f"gw {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="Save the gateway URL and API key")
    p.add_argument("--url")
    p.add_argument("--key", help="API key (prefer the prompt or GATEWAY_API_KEY; flags show in shell history)")
    p.add_argument("--no-verify", action="store_true", help="Save without calling the gateway")
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="Delete saved credentials").set_defaults(func=cmd_logout)

    sub.add_parser("models", parents=[common], help="List models you may use").set_defaults(func=cmd_models)

    p = sub.add_parser("chat", parents=[common], help="Send a prompt ('-' reads stdin)")
    p.add_argument("prompt")
    p.add_argument("--model")
    p.add_argument("--system")
    p.add_argument("--temperature", type=float)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--tool", action="append", help="Tool name to request (repeatable)")
    p.add_argument("--data-source", action="append", help="Data source to request (repeatable)")
    p.add_argument("--stream", action="store_true", help="Print the answer as it arrives")
    p.add_argument("--json-schema", metavar="FILE", help="Require the reply to match this JSON schema file")
    p.add_argument("--bypass-cache", action="store_true")
    p.add_argument("--quiet", action="store_true", help="Do not print the cache/model line")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("eval", parents=[common], help="Run an eval file; exit 1 if any case fails")
    p.add_argument("file", help="JSON, JSONL or YAML cases")
    p.add_argument("--fail-fast", action="store_true")
    p.set_defaults(func=cmd_eval)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        _err(f"error: {exc}")
        return 2
    except StructuredOutputError as exc:
        _err(f"error: the model's reply did not match the schema ({exc})")
        return 3
    except GatewayError as exc:
        _err(f"error: {type(exc).__name__}: {exc}")
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
