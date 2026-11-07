"""Semantic cache endpoints with the enterprise auth layer in front.

Uses the real RBAC engine, API-key provider and audit middleware; only the cache, Ollama and
the identity system's role lookup are faked.
"""
import json

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
from app.config.settings import Settings as AuthSettings
from app.middleware.audit import AuditMiddleware
from app.models.api_key import ApiKey  # noqa: F401
from app.models.audit_log import AuditLog
from semantic_router.analytics import AnalyticsStore
from semantic_router.api import SemanticGateway
from semantic_router.config import Settings

SECRET = "test-server-secret"
OWNER_ROLES: dict[str, set[str]] = {}
PROMPT = "What is 2 + 2?"
SECRET_PROMPT = "my-very-secret-prompt-text"


async def owner_roles(subject, auth_method):
    roles = OWNER_ROLES.get(subject)
    return None if roles is None else frozenset(roles)


class FakeCache:
    def __init__(self):
        self.entries = {}

    async def lookup(self, vector, scope_id):
        return self.entries.get(scope_id)

    async def store(self, vector, scope_id, category, model, entry):
        self.entries[scope_id] = entry


class FakeOllama:
    def __init__(self):
        self.chat_calls = []
        self.embed_calls = 0

    async def embed(self, model, text):
        self.embed_calls += 1
        return [0.1, 0.2, 0.3]

    async def chat(self, model, messages, options):
        self.chat_calls.append(model)
        return {"message": {"content": "answer"}, "prompt_eval_count": 8, "eval_count": 3}

    async def close(self):
        pass


class Env:
    def __init__(self, enforce=True, baseline="large-1"):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(bind=engine)
        self.ollama = FakeOllama()
        self.cache = FakeCache()
        settings = Settings(
            simple_model="small-1",
            reasoning_model="small-2",
            coding_model="small-3",
            baseline_model=baseline,
            model_prices={
                "small-1": {"input_per_million": 1, "output_per_million": 2},
                "small-2": {"input_per_million": 3, "output_per_million": 3},
                "large-1": {"input_per_million": 2, "output_per_million": 4},
            },
        )
        gateway = SemanticGateway(settings, self.cache, self.ollama, AnalyticsStore(":memory:"))
        app = FastAPI(lifespan=gateway.lifespan)
        app.add_middleware(AuditMiddleware, session_factory=self.factory)
        app.include_router(gateway.router)
        auth = AuthSettings(
            _env_file=None, api_key_hmac_secret=SecretStr(SECRET), auth_enforce=enforce
        )
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

    def chat(self, token, content=PROMPT, headers=None, **body):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": content}], **body},
            headers={**h, **(headers or {})},
        )

    def audit_rows(self):
        with self.factory() as db:
            return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


@pytest.fixture()
def env():
    e = Env()
    yield e
    e.client.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---- the auth layer is in front ------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/chat/completions"),
        ("post", "/v1/feedback"),
        ("get", "/dashboard"),
        ("get", "/dashboard/ui"),
        ("get", "/reports/cost"),
    ],
)
def test_every_semantic_endpoint_requires_authentication(env, method, path):
    r = getattr(env.client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert env.ollama.embed_calls == 0 and not env.ollama.chat_calls


def test_bad_key_is_rejected_before_any_model_work(env):
    assert env.chat("gk_deadbeef.nope").status_code == 401
    assert env.ollama.embed_calls == 0 and not env.ollama.chat_calls


def test_authenticated_viewer_gets_miss_then_hit(env):
    token = env.key(["viewer"])
    first, second = env.chat(token), env.chat(token)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
    assert env.ollama.chat_calls == ["small-1"]


def test_bypass_headers_still_work_behind_auth(env):
    token = env.key(["viewer"])
    env.chat(token)
    assert env.chat(token, headers={"x-cache-bypass": "true"}).headers["x-cache"] == "BYPASS"
    assert env.chat(token, headers={"Cache-Control": "no-store"}).headers["x-cache"] == "BYPASS"
    assert len(env.ollama.chat_calls) == 3


# ---- authorization --------------------------------------------------------------------------

def test_router_falls_back_to_a_model_the_role_may_use(env):
    # Baseline is large-1, which viewers may not invoke; the router must pick small-1 instead.
    r = env.chat(env.key(["viewer"]))
    assert r.headers["x-routed-model"] == "small-1"


def test_engineer_may_use_baseline_model(env):
    r = env.chat(env.key(["engineer"]))
    assert r.status_code == 200 and r.headers["x-routed-model"] == "large-1"


def test_explicitly_requested_forbidden_model_is_403_and_not_executed(env):
    r = env.chat(env.key(["viewer"]), model="large-1")
    assert r.status_code == 403
    assert env.ollama.embed_calls == 0 and not env.ollama.chat_calls


def test_forbidden_tool_and_datasource_are_403(env):
    token = env.key(["viewer"])
    assert env.chat(token, tools=["web-search"]).status_code == 403
    assert env.chat(token, data_sources=["finance-dw"]).status_code == 403
    assert not env.ollama.chat_calls


def test_analyst_may_use_granted_tool(env):
    assert env.chat(env.key(["analyst"]), tools=["web-search"]).status_code == 200


def test_api_key_scope_narrows_models(env):
    token = env.key(["admin"], scopes=["model:small-*"])
    assert env.chat(token, model="large-1").status_code == 403
    assert env.chat(token).headers["x-routed-model"] == "small-1"


@pytest.mark.parametrize("field,value", [("model", "*"), ("tools", ["*"]), ("data_sources", ["../x"])])
def test_wildcards_in_request_are_rejected(env, field, value):
    assert env.chat(env.key(["admin"]), **{field: value}).status_code == 422
    assert not env.ollama.chat_calls


def test_no_role_means_no_access(env):
    token = env.key(["viewer"])
    OWNER_ROLES["user-1"] = set()
    assert env.chat(token).status_code == 401


# ---- reports and feedback ---------------------------------------------------------------

def test_reports_and_dashboard_are_role_gated(env):
    viewer, analyst = env.key(["viewer"], owner="u1"), env.key(["analyst"], owner="u2")
    for path in ("/dashboard", "/dashboard/ui", "/reports/cost"):
        assert env.client.get(path, headers=bearer(viewer)).status_code == 403
        assert env.client.get(path, headers=bearer(analyst)).status_code == 200
    assert "Cache hit rate" in env.client.get("/dashboard/ui", headers=bearer(analyst)).text


def test_dashboard_reflects_cache_hits(env):
    token = env.key(["analyst"])
    env.chat(token), env.chat(token)
    assert env.client.get("/dashboard", headers=bearer(token)).json()["cache_hit_rate"] == 0.5


def test_feedback_requires_feedback_permission(env):
    request_id = env.chat(env.key(["analyst"], owner="u2")).json()["id"]
    body = {"request_id": request_id, "quality_score": 4}
    assert env.client.post("/v1/feedback", json=body, headers=bearer(env.key(["viewer"]))).status_code == 403
    assert env.client.post("/v1/feedback", json=body, headers=bearer(env.key(["analyst"], owner="u2"))).status_code == 204
    missing = {"request_id": "nope", "quality_score": 4}
    assert env.client.post("/v1/feedback", json=missing, headers=bearer(env.key(["analyst"], owner="u2"))).status_code == 404


# ---- cache isolation --------------------------------------------------------------------

def test_cache_is_not_shared_across_roles(env):
    a = env.key(["analyst"], owner="u1")
    b = env.key(["engineer"], owner="u2")
    c = env.key(["analyst"], owner="u3")
    assert env.chat(a, headers={"x-request-id": "r1"}).headers["x-cache"] == "MISS"
    assert env.chat(b, headers={"x-request-id": "r1"}).headers["x-cache"] == "MISS"
    # Same tenant and roles share entries, as designed.
    assert env.chat(c, headers={"x-request-id": "r1"}).headers["x-cache"] == "HIT"


# ---- audit ------------------------------------------------------------------------------

def test_audit_rows_cover_allow_deny_and_unauthenticated(env):
    token = env.key(["viewer"])
    env.chat(token, content=SECRET_PROMPT)
    env.chat(token, content=SECRET_PROMPT)
    env.chat(token, model="large-1", content=SECRET_PROMPT)
    env.chat(None, content=SECRET_PROMPT)
    env.client.get("/dashboard", headers=bearer(token))

    rows = env.audit_rows()
    assert [r.decision for r in rows] == ["allow", "allow", "deny", "deny", "deny"]
    assert [r.status_code for r in rows] == [200, 200, 403, 401, 403]
    assert rows[2].resource == "model:large-1"
    assert rows[4].resource == "endpoint:reports" and rows[4].action.startswith("GET")

    first = json.loads(rows[0].details)["inference"]
    second = json.loads(rows[1].details)["inference"]
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert first["model"] == "small-1" and first["category"] == "simple"
    assert first["usage"] == {"prompt_tokens": 8, "completion_tokens": 3}
    assert rows[0].subject == "user-1" and rows[0].auth_method == "api_key"
    # Prompts and completions never reach the audit table.
    assert all(SECRET_PROMPT not in (r.details or "") for r in rows)
    assert all("answer" not in (r.details or "") for r in rows)


def test_audit_only_mode_does_not_block(env):
    e = Env(enforce=False)
    try:
        r = e.chat(e.key(["viewer"]), model="large-1")
        assert r.status_code == 200 and r.headers["x-routed-model"] == "large-1"
        row = e.audit_rows()[0]
        assert row.decision == "deny" and json.loads(row.details)["enforced"] is False
    finally:
        e.client.close()


# ---- lifecycle --------------------------------------------------------------------------

def test_readiness_is_503_until_warm_up_then_200():
    e = Env()
    try:
        assert e.client.get("/health/ready").status_code == 503  # lifespan has not run
    finally:
        e.client.close()
    e = Env()
    with e.client as client:  # entering the client runs the lifespan warm-up
        assert client.get("/health/ready").json() == {"status": "ready"}
