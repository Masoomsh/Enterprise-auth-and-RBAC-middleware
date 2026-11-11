import json
import os
import stat

import httpx
import pytest

from gateway_client import Client, RetryPolicy, cli


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_CONFIG_DIR", str(tmp_path / "cfg"))
    for name in ("GATEWAY_URL", "GATEWAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fake_remote(monkeypatch, handler):
    """Make every remote client talk to `handler` instead of the network."""
    seen = {}

    def factory(**kw):
        seen.update(kw)
        http = httpx.Client(base_url="http://gw", transport=httpx.MockTransport(handler))
        return Client(http_client=http, retry=RetryPolicy(max_retries=0), **kw)

    monkeypatch.setattr(cli, "_client_factory", factory)
    return seen


def chat_ok(req):
    if req.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
    return httpx.Response(200, headers={"x-cache": "HIT", "x-routed-model": "m1", "x-request-id": "r1"},
                          json={"choices": [{"message": {"content": "pong"}}], "usage": {"total_tokens": 3}})


# ---- local mode ------------------------------------------------------------------------

def test_chat_local(capsys):
    code, out, err = run(capsys, "chat", "hello there", "--local")
    assert code == 0 and out.strip() == "[local] hello there"
    assert "cache=MISS" in err and "request_id=" in err


def test_chat_local_stream_and_quiet(capsys):
    code, out, err = run(capsys, "chat", "a b c", "--local", "--stream", "--quiet")
    assert code == 0 and out.strip() == "[local] a b c" and err == ""


def test_chat_json_output(capsys):
    code, out, err = run(capsys, "chat", "hi", "--local", "--json")
    data = json.loads(out)
    assert code == 0 and data["content"] == "[local] hi" and data["cache"] == "MISS" and err == ""


def test_chat_with_json_schema_file(capsys, tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text(json.dumps({"type": "object", "required": ["city"],
                                  "properties": {"city": {"type": "string"}}}))
    code, out, _ = run(capsys, "chat", "where?", "--local", "--json-schema", str(schema))
    assert code == 0 and json.loads(out) == {"city": "sample"}


def test_chat_reads_stdin(capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    code, out, _ = run(capsys, "chat", "-", "--local", "--quiet")
    assert code == 0 and out.strip() == "[local] from stdin"


def test_models_local(capsys):
    assert run(capsys, "models", "--local")[1].split() == ["local-model"]
    assert json.loads(run(capsys, "models", "--local", "--json")[1]) == ["local-model"]


def test_eval_exit_codes(capsys, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"name": "a", "prompt": "x", "expect": {"contains": ["[local]"]}}]))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "a", "prompt": "x", "expect": {"contains": ["nope"]}}]))
    code, out, _ = run(capsys, "eval", str(good), "--local")
    assert code == 0 and "1/1 passed" in out
    code, out, _ = run(capsys, "eval", str(bad), "--local")
    assert code == 1 and "FAIL" in out and "missing text" in out
    code, out, _ = run(capsys, "eval", str(bad), "--local", "--json")
    assert code == 1 and json.loads(out)["passed"] == 0


def test_eval_missing_file_is_usage_error(capsys):
    code, _, err = run(capsys, "eval", "does-not-exist.json", "--local")
    assert code == 2 and "error:" in err


def test_empty_prompt_and_stream_with_schema_are_usage_errors(capsys, tmp_path):
    assert run(capsys, "chat", "  ", "--local")[0] == 2
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    assert run(capsys, "chat", "x", "--local", "--stream", "--json-schema", str(schema))[0] == 2


# ---- login / config / remote -----------------------------------------------------------

def test_unconfigured_remote_call_explains_what_to_do(capsys):
    code, _, err = run(capsys, "models")
    assert code == 2 and "gw login" in err


def test_login_verifies_saves_and_later_calls_use_it(capsys, monkeypatch):
    seen = fake_remote(monkeypatch, chat_ok)
    code, _, err = run(capsys, "login", "--url", "http://gw/", "--key", "gk_secret")
    assert code == 0 and "2 model(s)" in err and "gk_secret" not in err
    saved = json.loads(cli.config_path().read_text())
    assert saved == {"url": "http://gw", "api_key": "gk_secret"}
    if os.name != "nt":
        assert stat.S_IMODE(cli.config_path().stat().st_mode) == 0o600

    code, out, err = run(capsys, "chat", "ping")
    assert code == 0 and out.strip() == "pong" and "cache=HIT" in err
    assert seen["api_key"] == "gk_secret" and seen["base_url"] == "http://gw"


def test_login_with_wrong_key_saves_nothing(capsys, monkeypatch):
    fake_remote(monkeypatch, lambda r: httpx.Response(401, json={"detail": "bad key"}))
    code, _, err = run(capsys, "login", "--url", "http://gw", "--key", "gk_bad")
    assert code == 3 and "AuthError" in err and not cli.config_path().exists()


def test_login_no_verify_and_logout(capsys):
    assert run(capsys, "login", "--url", "http://gw", "--key", "k", "--no-verify")[0] == 0
    assert cli.config_path().exists()
    assert run(capsys, "logout")[0] == 0 and not cli.config_path().exists()
    assert "Nothing to remove" in run(capsys, "logout")[2]


def test_environment_beats_saved_config(capsys, monkeypatch):
    cli.save_config("http://saved", "saved-key")
    monkeypatch.setenv("GATEWAY_API_KEY", "env-key")
    seen = fake_remote(monkeypatch, chat_ok)
    run(capsys, "models", "--url", "http://flag")
    assert seen["api_key"] == "env-key" and seen["base_url"] == "http://flag"


def test_gateway_errors_map_to_exit_3_and_show_request_id(capsys, monkeypatch):
    cli.save_config("http://gw", "k")
    fake_remote(monkeypatch, lambda r: httpx.Response(
        403, json={"detail": "no model for you"}, headers={"x-request-id": "rid-7"}))
    code, out, err = run(capsys, "chat", "hi", "--model", "large")
    assert code == 3 and out == "" and "PermissionDenied" in err and "rid-7" in err
