import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.authentication.api_key import ApiKeyProvider, create_api_key
from app.authentication.dependencies import app_settings, get_providers
from app.config.database import Base, get_db
from app.config.settings import Settings
from app.models.api_key import ApiKey  # noqa: F401
from app.routes import admin_routes, auth_routes, inference_routes
from app.services import identity_service
from app.services.inference_service import get_inference_backend

SECRET = "test-server-secret"
calls: list[tuple] = []
# subject -> the roles that subject CURRENTLY holds (stand-in for SuperTokens)
OWNER_ROLES: dict[str, set[str]] = {}


async def current_owner_roles(subject, auth_method):
    roles = OWNER_ROLES.get(subject)
    return None if roles is None else frozenset(roles)


async def fake_backend(model, messages, tools, datasources):
    calls.append((model, tools, datasources))
    return {"content": "hello", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def build(enforce=True):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    app = FastAPI()
    for r in (inference_routes.router, auth_routes.router, admin_routes.router):
        app.include_router(r)

    def db():
        with factory() as session:
            yield session

    settings = Settings(_env_file=None, api_key_hmac_secret=SecretStr(SECRET), auth_enforce=enforce)
    app.dependency_overrides[app_settings] = lambda: settings
    app.dependency_overrides[get_providers] = lambda: [ApiKeyProvider(factory, SECRET, current_owner_roles)]
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_inference_backend] = lambda: fake_backend
    calls.clear()
    OWNER_ROLES.clear()
    return app, factory


@pytest.fixture()
def env():
    app, factory = build()
    return TestClient(app), factory


def key(factory, roles, scopes, owner="user-1", owner_holds=None):
    """Create a key. By default the owner currently holds exactly the key's roles."""
    OWNER_ROLES[owner] = set(roles if owner_holds is None else owner_holds)
    with factory() as db:
        return create_api_key(
            db, SECRET, owner_subject=owner, name="t", roles=roles, scopes=scopes
        )[1]


def call(client, token, **body):
    payload = {"model": "small-1", "messages": [{"role": "user", "content": "hi"}], **body}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/v1/inference", json=payload, headers=headers)


# ---- authentication ----------------------------------------------------------

def test_no_credentials_401(env):
    client, _ = env
    r = call(client, None)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert not calls


def test_bad_key_401_and_backend_untouched(env):
    client, _ = env
    assert call(client, "gk_deadbeef.nope").status_code == 401
    assert not calls


def test_unauthenticated_request_gets_401_before_body_validation(env):
    client, _ = env
    assert client.post("/v1/inference", json={"nonsense": True}).status_code == 401


# ---- authorization -----------------------------------------------------------

def test_allowed_call_reaches_backend(env):
    client, factory = env
    r = call(client, key(factory, ["viewer"], ["model:small-*"]))
    assert r.status_code == 200 and r.json()["content"] == "hello"
    assert calls == [("small-1", [], [])]


def test_role_denies_model(env):
    client, factory = env
    r = call(client, key(factory, ["viewer"], ["*"]), model="gpt-4o")
    assert r.status_code == 403 and not calls


def test_scope_denies_model_even_for_admin(env):
    client, factory = env
    r = call(client, key(factory, ["admin"], ["model:small-*"]), model="gpt-4o")
    assert r.status_code == 403 and not calls


def test_tools_and_datasources_are_checked(env):
    client, factory = env
    token = key(factory, ["analyst"], ["*"])
    assert call(client, token, tools=["web-search"], datasources=["analytics-eu"]).status_code == 200
    assert call(client, token, tools=["shell"]).status_code == 403
    assert call(client, token, datasources=["finance-dw"]).status_code == 403
    assert len(calls) == 1  # only the first request reached the backend


def test_one_denied_resource_blocks_whole_request(env):
    client, factory = env
    token = key(factory, ["analyst"], ["*"])
    r = call(client, token, tools=["web-search", "shell"])
    assert r.status_code == 403 and not calls


@pytest.mark.parametrize("field,value", [("model", "*"), ("model", "../x"), ("tools", ["*"])])
def test_wildcards_and_traversal_rejected(env, field, value):
    client, factory = env
    r = call(client, key(factory, ["admin"], ["*"]), **{field: value})
    assert r.status_code == 422 and not calls


def test_audit_only_mode_logs_but_allows():
    app, factory = build(enforce=False)
    client = TestClient(app)
    r = call(client, key(factory, ["viewer"], ["*"]), model="gpt-4o")
    assert r.status_code == 200 and len(calls) == 1


# ---- API key self-service ----------------------------------------------------

def test_key_cannot_create_keys(env):
    client, factory = env
    token = key(factory, ["admin"], ["*"])
    r = client.post(
        "/v1/api-keys",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "x", "roles": ["admin"], "scopes": ["*"]},
    )
    assert r.status_code == 403


# ---- admin routes ------------------------------------------------------------

@pytest.fixture()
def fake_identity_service(monkeypatch):
    state: dict[str, list[str]] = {}

    async def assign(user_id, role, tenant_id="public"):
        state.setdefault(user_id, []).append(role)

    async def revoke(user_id, role, tenant_id="public"):
        state[user_id].remove(role)

    async def list_(user_id, tenant_id="public"):
        return state.get(user_id, [])

    monkeypatch.setattr(identity_service, "assign_role", assign)
    monkeypatch.setattr(identity_service, "revoke_role", revoke)
    monkeypatch.setattr(identity_service, "list_roles", list_)
    return state


def test_admin_can_manage_roles(env, fake_identity_service):
    client, factory = env
    h = {"Authorization": f"Bearer {key(factory, ['admin'], ['*'])}"}
    assert client.post("/admin/users/u-2/roles", json={"role": "analyst"}, headers=h).status_code == 200
    assert fake_identity_service == {"u-2": ["analyst"]}
    assert client.delete("/admin/users/u-2/roles/analyst", headers=h).status_code == 200
    assert client.post("/admin/users/u-2/roles", json={"role": "root"}, headers=h).status_code == 422


@pytest.mark.parametrize("role", ["engineer", "analyst", "viewer"])
def test_non_admin_cannot_manage_roles(env, fake_identity_service, role):
    client, factory = env
    h = {"Authorization": f"Bearer {key(factory, [role], ['*'])}"}
    assert client.post("/admin/users/u-2/roles", json={"role": "admin"}, headers=h).status_code == 403
    assert client.get("/admin/users/u-2/roles", headers=h).status_code == 403
    assert fake_identity_service == {}


def test_admin_scoped_key_cannot_reach_admin_routes_outside_scope(env, fake_identity_service):
    client, factory = env
    h = {"Authorization": f"Bearer {key(factory, ['admin'], ['model:small-*'])}"}
    assert client.post("/admin/users/u-2/roles", json={"role": "viewer"}, headers=h).status_code == 403


# ---- role changes take effect on existing keys immediately --------------------

def test_demoting_the_owner_narrows_existing_keys(env):
    client, factory = env
    token = key(factory, ["analyst", "viewer"], ["*"])
    assert call(client, token, tools=["web-search"]).status_code == 200
    OWNER_ROLES["user-1"] = {"viewer"}  # analyst role removed in the identity system
    assert call(client, token, tools=["web-search"]).status_code == 403  # viewer: no tools
    assert call(client, token).status_code == 200  # viewer can still use small models


def test_removing_all_owner_roles_disables_existing_keys(env):
    client, factory = env
    token = key(factory, ["analyst"], ["*"])
    assert call(client, token).status_code == 200
    OWNER_ROLES["user-1"] = set()
    r = call(client, token)
    assert r.status_code == 401 and len(calls) == 1


def test_deleted_owner_disables_existing_keys(env):
    client, factory = env
    token = key(factory, ["analyst"], ["*"])
    del OWNER_ROLES["user-1"]  # identity system no longer knows the owner
    assert call(client, token).status_code == 401
