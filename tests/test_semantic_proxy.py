"""Auth in front of the stand-alone semantic cache app (proxy mode), with a fake upstream."""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.authentication.api_key import ApiKeyProvider, create_api_key
from app.authentication.dependencies import app_settings, get_providers
from app.config.database import Base
from app.config.settings import Settings
from app.middleware.audit import AuditMiddleware
from app.models.api_key import ApiKey  # noqa: F401
from app.models.audit_log import AuditLog
from app.routes import semantic_proxy

SECRET = "test-server-secret"
OWNER_ROLES: dict[str, set[str]] = {}


async def owner_roles(subject, auth_method):
    roles = OWNER_ROLES.get(subject)
    return None if roles is None else frozenset(roles)


class Env:
    def __init__(self, routed="small-1", upstream_down=False):
        self.seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if upstream_down:
                raise httpx.ConnectError("down")
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(
                    200,
                    json={"id": "r1", "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                          "choices": [{"message": {"content": "ANSWER-TEXT"}}]},
                    headers={"x-cache": "HIT", "x-routed-model": routed,
                             "x-query-complexity": "simple", "x-experiment-arm": "control",
                             "set-cookie": "leak=1"},
                )
            if request.url.path == "/v1/feedback":
                return httpx.Response(204)
            if request.url.path == "/health/ready":
                return httpx.Response(200, json={"status": "ready"})
            return httpx.Response(200, json={"cache_hit_rate": 0.5})

        client = httpx.AsyncClient(
            base_url="http://upstream", transport=httpx.MockTransport(handler)
        )
        router, lifespan = semantic_proxy.build_router(
            semantic_proxy.SemanticUpstream("http://upstream", client)
        )
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(bind=engine)
        app = FastAPI(lifespan=lifespan)
        app.add_middleware(AuditMiddleware, session_factory=self.factory)
        app.include_router(router)
        auth = Settings(_env_file=None, api_key_hmac_secret=SecretStr(SECRET))
        app.dependency_overrides[app_settings] = lambda: auth
        app.dependency_overrides[get_providers] = lambda: [
            ApiKeyProvider(self.factory, SECRET, owner_roles)
        ]
        OWNER_ROLES.clear()
        self.client = TestClient(app)

    def key(self, roles, scopes=("*",), owner="user-1"):
        OWNER_ROLES[owner] = set(roles)
        with self.factory() as db:
            return create_api_key(
                db, SECRET, owner_subject=owner, name="t", roles=list(roles), scopes=list(scopes)
            )[1]

    def chat(self, token, **body):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "SECRET-PROMPT"}], **body},
            headers=h,
        )

    def audit(self):
        with self.factory() as db:
            return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


@pytest.fixture()
def env():
    e = Env()
    yield e
    e.client.close()


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.parametrize(
    "method,path",
    [("post", "/v1/chat/completions"), ("post", "/v1/feedback"), ("get", "/dashboard"),
     ("get", "/dashboard/ui"), ("get", "/reports/cost")],
)
def test_unauthenticated_requests_never_reach_upstream(env, method, path):
    r = getattr(env.client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401
    assert env.seen == []


def test_bad_credential_never_reaches_upstream(env):
    assert env.chat("gk_deadbeef.nope").status_code == 401
    assert env.seen == []


def test_authorized_chat_is_forwarded_without_credentials(env):
    r = env.chat(env.key(["viewer"]))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ANSWER-TEXT"
    assert r.headers["x-cache"] == "HIT" and r.headers["x-routed-model"] == "small-1"
    assert "set-cookie" not in r.headers  # upstream cookies are not relayed
    (sent,) = env.seen
    assert "authorization" not in sent.headers and "cookie" not in sent.headers


def test_forbidden_requested_model_tool_datasource_not_forwarded(env):
    token = env.key(["viewer"])
    assert env.chat(token, model="large-1").status_code == 403
    assert env.chat(token, tools=["shell"]).status_code == 403
    assert env.chat(token, data_sources=["finance-dw"]).status_code == 403
    assert env.seen == []


@pytest.mark.parametrize("body", [{"model": "*"}, {"tools": ["*"]}, {"data_sources": ["../x"]}])
def test_wildcards_rejected(env, body):
    assert env.chat(env.key(["admin"]), **body).status_code == 422
    assert env.seen == []


def test_answer_from_a_model_the_caller_may_not_use_is_withheld():
    e = Env(routed="large-1")  # upstream router picked a model viewers cannot invoke
    try:
        r = e.chat(e.key(["viewer"]))
        assert r.status_code == 403 and "ANSWER-TEXT" not in r.text
        row = e.audit()[0]
        assert row.decision == "deny" and row.resource == "model:large-1"
    finally:
        e.client.close()


def test_engineer_may_receive_large_model_answer():
    e = Env(routed="large-1")
    try:
        assert e.chat(e.key(["engineer"])).status_code == 200
    finally:
        e.client.close()


def test_reports_and_feedback_are_role_gated(env):
    viewer, analyst = env.key(["viewer"], owner="a"), env.key(["analyst"], owner="b")
    for path in ("/dashboard", "/dashboard/ui", "/reports/cost"):
        assert env.client.get(path, headers=bearer(viewer)).status_code == 403
        assert env.client.get(path, headers=bearer(analyst)).status_code == 200
    body = {"request_id": "r1", "quality_score": 4}
    assert env.client.post("/v1/feedback", json=body, headers=bearer(viewer)).status_code == 403
    assert env.client.post("/v1/feedback", json=body, headers=bearer(analyst)).status_code == 204


def test_scoped_key_cannot_exceed_scope(env):
    token = env.key(["admin"], scopes=["model:small-*"])
    assert env.client.get("/dashboard", headers=bearer(token)).status_code == 403
    assert env.chat(token, model="large-1").status_code == 403


def test_upstream_down_is_502_and_audited():
    e = Env(upstream_down=True)
    try:
        assert e.chat(e.key(["viewer"])).status_code == 502
        assert e.audit()[0].status_code == 502
    finally:
        e.client.close()


def test_audit_has_metadata_but_no_prompt_or_answer(env):
    env.chat(env.key(["viewer"]))
    row = env.audit()[0]
    meta = json.loads(row.details)["inference"]
    assert row.decision == "allow" and row.auth_method == "api_key"
    assert meta["model"] == "small-1" and meta["cache_hit"] is True
    assert meta["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}
    assert "SECRET-PROMPT" not in row.details and "ANSWER-TEXT" not in row.details
