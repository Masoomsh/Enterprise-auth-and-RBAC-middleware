"""Prompt evals: run a file of cases against any client (remote or local) and report pass/fail.

A case file is JSON (a list), JSONL, or YAML (needs the `yaml` extra):

    - name: capital
      prompt: What is the capital of France?
      expect:
        contains: [Paris]
        not_contains: [London]
        regex: "(?i)paris"
        json_schema: {type: object, required: [city]}
        max_latency_s: 5
        cache: MISS            # HIT or MISS
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .errors import GatewayError


@dataclass
class EvalCase:
    name: str
    prompt: str
    system: str | None = None
    model: str | None = None
    expect: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: list[str]
    latency_s: float
    cache: str = ""
    model: str = ""
    content: str = ""


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def cache_hits(self) -> int:
        return sum(r.cache == "HIT" for r in self.results)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "passed": self.passed, "pass_rate": self.pass_rate,
                "cache_hits": self.cache_hits, "results": [asdict(r) for r in self.results]}

    def summary(self) -> str:
        lines = []
        for r in self.results:
            lines.append(f"{'PASS' if r.passed else 'FAIL'}  {r.name}  ({r.latency_s * 1000:.0f} ms, {r.cache or '-'})")
            lines += [f"      - {f}" for f in r.failures]
        lines.append(f"{self.passed}/{self.total} passed, {self.cache_hits} cache hits")
        return "\n".join(lines)


def load_cases(path: str | Path) -> list[EvalCase]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML case files need: pip install 'gateway-client[yaml]'") from exc
        raw = yaml.safe_load(text)
    elif p.suffix == ".jsonl":
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        raw = json.loads(text)
    if not isinstance(raw, list) or not raw:
        raise ValueError("A case file must contain a non-empty list of cases")
    cases = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or "prompt" not in item:
            raise ValueError(f"Case {i} needs a 'prompt'")
        cases.append(EvalCase(
            name=str(item.get("name") or f"case-{i}"), prompt=item["prompt"], system=item.get("system"),
            model=item.get("model"), expect=item.get("expect") or {},
        ))
    return cases


def check(content: str, expect: dict[str, Any], latency_s: float, cache: str) -> list[str]:
    failures: list[str] = []
    for needle in expect.get("contains", []):
        if needle not in content:
            failures.append(f"missing text: {needle!r}")
    for needle in expect.get("not_contains", []):
        if needle in content:
            failures.append(f"unexpected text: {needle!r}")
    if "equals" in expect and content.strip() != str(expect["equals"]).strip():
        failures.append("reply does not equal the expected text")
    if "regex" in expect and not re.search(expect["regex"], content):
        failures.append(f"regex did not match: {expect['regex']!r}")
    if "json_schema" in expect:
        try:
            Draft202012Validator(expect["json_schema"]).validate(json.loads(content))
        except ValueError:
            failures.append("reply is not valid JSON")
        except ValidationError as exc:
            failures.append(f"JSON schema: {exc.message}")
    if "max_latency_s" in expect and latency_s > expect["max_latency_s"]:
        failures.append(f"too slow: {latency_s:.2f}s > {expect['max_latency_s']}s")
    if "cache" in expect and cache != expect["cache"]:
        failures.append(f"cache was {cache}, expected {expect['cache']}")
    return failures


def run_eval(client: Any, cases: list[EvalCase], *, fail_fast: bool = False) -> EvalReport:
    results: list[CaseResult] = []
    for case in cases:
        start = time.perf_counter()
        try:
            reply = client.chat(case.prompt, system=case.system, model=case.model)
        except GatewayError as exc:
            results.append(CaseResult(case.name, False, [f"{type(exc).__name__}: {exc}"],
                                      time.perf_counter() - start))
        else:
            latency = time.perf_counter() - start
            failures = check(reply.content, case.expect, latency, reply.cache)
            results.append(CaseResult(case.name, not failures, failures, latency, reply.cache,
                                      reply.model, reply.content))
        if fail_fast and not results[-1].passed:
            break
    return EvalReport(results)
