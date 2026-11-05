import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.middleware.audit import AuditMiddleware
from app.models.audit_log import AuditLog
from app.services.audit_service import should_audit
from tests.test_inference import build, key

PROMPT = "TOP-SECRET-PROMPT-TEXT"


@pytest.fixture()
def env():
    app, factory = build()
    app.add_middleware(AuditMiddleware, session_factory=factory)
    return TestClient(app), factory


def rows(factory):
    with factory() as db:
        return db.query(AuditLog).order_by(AuditLog.id).all()


def infer(client, token, model="small-1", **extra):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], **extra}
    return client.post("/v1/inference", json=body, headers=headers)


def test_allowed_call_is_audited(env):
    client, factory = env
    token = key(factory, ["viewer"], ["model:small-*"])
    r = infer(client, token)
    (row,) = rows(factory)
    assert r.status_code == 200
    assert (row.decision, row.status_code) == ("allow", 200)
    assert (row.subject, row.auth_method) == ("user-1", "api_key")
    assert row.token_id == "gk_" + token[3:11]
    assert (row.action, row.resource) == ("inference.invoke", "model:small-1")
    assert row.request_id == r.headers["x-request-id"]
    details = json.loads(row.details)
    assert details["inference"]["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    assert details["inference"]["prompt_chars"] == len(PROMPT)


def test_role_denial_is_audited_with_reason(env):
    client, factory = env
    r = infer(client, key(factory, ["viewer"], ["*"]), model="gpt-4o")
    (row,) = rows(factory)
    assert r.status_code == 403
    assert (row.decision, row.status_code, row.resource) == ("deny", 403, "model:gpt-4o")
    assert "no role grants" in row.reason


def test_scope_denial_is_audited(env):
    client, factory = env
    infer(client, key(factory, ["admin"], ["model:small-*"]), model="gpt-4o")
    (row,) = rows(factory)
    assert row.decision == "deny" and "scope" in row.reason


def test_missing_credentials_are_audited(env):
    client, factory = env
    infer(client, None)
    (row,) = rows(factory)
    assert (row.decision, row.status_code) == ("deny", 401)
    assert row.subject is None and row.auth_method == "none"
    assert row.reason == "no credentials presented"


def test_bad_credentials_are_audited_without_the_secret(env):
    client, factory = env
    infer(client, "gk_deadbeef.super-secret-value")
    (row,) = rows(factory)
    assert (row.decision, row.status_code) == ("deny", 401)
    assert row.reason.startswith("api_key:")
    assert "super-secret-value" not in json.dumps(
        {c.name: str(getattr(row, c.name)) for c in AuditLog.__table__.columns}
    )


def test_validation_error_after_authentication_is_audited_as_error(env):
    client, factory = env
    infer(client, key(factory, ["admin"], ["*"]), model="*")
    (row,) = rows(factory)
    assert (row.decision, row.status_code) == ("error", 422)
    assert row.subject == "user-1"


def test_exactly_one_row_per_request(env):
    client, factory = env
    good = key(factory, ["analyst"], ["*"])
    infer(client, good)
    infer(client, good, model="gpt-4o")
    infer(client, good, tools=["web-search", "shell"])
    infer(client, None)
    infer(client, "gk_bad.bad")
    infer(client, good, model="*")
    assert len(rows(factory)) == 6


def test_prompt_content_never_stored(env):
    client, factory = env
    infer(client, key(factory, ["viewer"], ["*"]))
    infer(client, key(factory, ["viewer"], ["*"]), model="gpt-4o")
    for row in rows(factory):
        blob = json.dumps({c.name: str(getattr(row, c.name)) for c in AuditLog.__table__.columns})
        assert PROMPT not in blob


def test_multiple_resources_all_recorded(env):
    client, factory = env
    infer(client, key(factory, ["analyst"], ["*"]), tools=["web-search", "shell"])
    (row,) = rows(factory)
    decisions = json.loads(row.details)["decisions"]
    assert [(d["resource"], d["allowed"]) for d in decisions] == [
        ("model:small-1", True),
        ("tool:shell", False),
        ("tool:web-search", True),
    ]
    assert row.decision == "deny" and row.resource == "tool:shell"


def test_admin_routes_are_audited(env):
    client, factory = env
    token = key(factory, ["viewer"], ["*"])
    r = client.get("/admin/users/u-2/roles", headers={"Authorization": f"Bearer {token}"})
    (row,) = rows(factory)
    assert r.status_code == 403
    assert (row.decision, row.resource) == ("deny", "admin:roles")
    assert row.action == "GET /admin/users/{user_id}/roles"


def test_audit_only_mode_records_would_be_denial():
    app, factory = build(enforce=False)
    app.add_middleware(AuditMiddleware, session_factory=factory)
    client = TestClient(app)
    r = infer(client, key(factory, ["viewer"], ["*"]), model="gpt-4o")
    (row,) = rows(factory)
    assert r.status_code == 200 and row.decision == "deny"
    assert json.loads(row.details)["enforced"] is False


def test_audit_write_failure_does_not_break_response_and_is_logged(env, caplog):
    client, factory = env
    token = key(factory, ["viewer"], ["*"])

    def broken():
        raise RuntimeError("db down")

    client.app.user_middleware.clear()
    client.app.middleware_stack = None
    client.app.add_middleware(AuditMiddleware, session_factory=broken)
    with caplog.at_level(logging.ERROR, logger="audit"):
        r = infer(client, token)
    assert r.status_code == 200
    assert "AUDIT_WRITE_FAILED" in caplog.text and "model:small-1" in caplog.text


def test_audit_rows_are_immutable(env):
    client, factory = env
    infer(client, None)
    with factory() as db:
        row = db.query(AuditLog).one()
        row.decision = "allow"
        with pytest.raises(RuntimeError, match="insert-only"):
            db.commit()
    with factory() as db:
        with pytest.raises(RuntimeError, match="insert-only"):
            db.delete(db.query(AuditLog).one())
            db.commit()


@pytest.mark.parametrize(
    "path,method,expected",
    [
        ("/v1/inference", "POST", True),
        ("/admin/users/x/roles", "GET", True),
        ("/v1/inference", "OPTIONS", False),
        ("/health", "GET", False),
        ("/auth/signin", "POST", False),
        ("/api/me", "GET", False),
    ],
)
def test_should_audit(path, method, expected):
    assert should_audit(path, method) is expected
