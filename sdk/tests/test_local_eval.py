import json

import pytest
from pydantic import BaseModel

from gateway_client import (
    AsyncClient,
    Client,
    EvalCase,
    LocalGateway,
    StructuredOutputError,
    load_cases,
    run_eval,
)


class City(BaseModel):
    city: str
    population: int


def test_local_mode_needs_no_url_or_key_and_caches():
    with Client(mode="local") as c:
        a = c.chat("What is 2+2?")
        b = c.chat("What is 2+2?")
        assert a.cache == "MISS" and b.cache == "HIT" and a.content == "[local] What is 2+2?"
        assert c.chat("What is 2+2?", bypass_cache=True).cache == "BYPASS"
        assert c.models() == ["local-model"]


def test_local_streaming():
    with Client(mode="local") as c:
        s = c.chat("one two three", stream=True)
        assert "".join(s) == "[local] one two three" and s.usage["total_tokens"] > 0


def test_local_default_responder_satisfies_a_schema():
    with Client(mode="local") as c:
        assert c.chat("anything", response_model=City).parsed == City(city="sample", population=0)


def test_local_enforces_schema_like_the_gateway():
    gw = LocalGateway(responder=lambda m, s: "definitely not json")
    with Client(mode="local", local=gw) as c:
        with pytest.raises(StructuredOutputError):
            c.chat("x", response_model=City)  # gateway 422 -> one retry -> still bad -> error
        assert gw.calls == 2


async def test_local_async():
    async with AsyncClient(mode="local") as c:
        assert (await c.chat("hi")).content == "[local] hi"


# ---- eval ------------------------------------------------------------------------------

def test_eval_passes_and_fails_with_reasons():
    gw = LocalGateway(responder=lambda m, s: "Paris is the capital of France")
    cases = [
        EvalCase("ok", "capital?", expect={"contains": ["Paris"], "regex": "(?i)capital", "cache": "MISS"}),
        EvalCase("bad", "capital again?", expect={"contains": ["London"], "not_contains": ["Paris"]}),
        EvalCase("hit", "capital?", expect={"cache": "HIT"}),
    ]
    with Client(mode="local", local=gw) as c:
        report = run_eval(c, cases)
    assert [r.passed for r in report.results] == [True, False, True]
    assert report.passed == 2 and report.cache_hits == 1 and not report.ok
    assert "missing text: 'London'" in report.results[1].failures[0]
    assert "2/3 passed" in report.summary()


def test_eval_json_schema_check_and_fail_fast():
    gw = LocalGateway(responder=lambda m, s: '{"city": 5}')
    cases = [EvalCase("a", "q1", expect={"json_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}),
             EvalCase("b", "q2")]
    with Client(mode="local", local=gw) as c:
        report = run_eval(c, cases, fail_fast=True)
    assert report.total == 1 and "JSON schema" in report.results[0].failures[0]


def test_eval_records_gateway_errors_as_failures():
    import httpx

    from gateway_client import RetryPolicy

    http = httpx.Client(base_url="http://gw", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"detail": "denied"})))
    c = Client("http://gw", "gk", http_client=http, retry=RetryPolicy(max_retries=0))
    report = run_eval(c, [EvalCase("x", "hi")])
    assert not report.results[0].passed and "PermissionDenied" in report.results[0].failures[0]


def test_load_cases_json_jsonl_yaml(tmp_path):
    items = [{"name": "n", "prompt": "p", "expect": {"contains": ["x"]}}]
    (tmp_path / "c.json").write_text(json.dumps(items))
    (tmp_path / "c.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    (tmp_path / "c.yaml").write_text("- name: n\n  prompt: p\n  expect:\n    contains: [x]\n")
    for name in ("c.json", "c.jsonl", "c.yaml"):
        (case,) = load_cases(tmp_path / name)
        assert case.name == "n" and case.expect == {"contains": ["x"]}
    (tmp_path / "bad.json").write_text("[{}]")
    with pytest.raises(ValueError):
        load_cases(tmp_path / "bad.json")
