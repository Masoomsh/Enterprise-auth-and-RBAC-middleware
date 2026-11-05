import asyncio

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from supertokens_python.recipe.session.exceptions import TryRefreshTokenError
from starlette.requests import Request

from app.authentication import session as session_module
from app.authentication.base import AuthenticationError, Identity
from app.authentication.dependencies import app_settings, get_identity, get_providers
from app.authentication.session import SuperTokensSessionProvider
from app.config.settings import Settings
from app.services import identity_service
from tests.test_inference import OWNER_ROLES, build, key


# ---- session provider --------------------------------------------------------

class FakeSession:
    def get_user_id(self):
        return "st-user-1"

    def get_handle(self):
        return "handle-1"

    def get_access_token_payload(self):
        return {"st-role": {"v": ["analyst"], "t": 0}, "tenant": "acme", "aud": "semantic-gateway"}


def _req(headers=None):
    return Request({"type": "http", "headers": headers or []})


def test_session_provider_maps_payload_to_identity(monkeypatch):
    async def fake_get_session(request, session_required=True):
        assert session_required is False
        return FakeSession()

    monkeypatch.setattr(session_module, "get_session", fake_get_session)
    provider = SuperTokensSessionProvider(Settings())
    identity = asyncio.run(provider.authenticate(_req([(b"cookie", b"sAccessToken=abc")])))
    assert identity == Identity(
        subject="st-user-1",
        tenant="acme",
        auth_method="session",
        roles=frozenset({"analyst"}),
        scopes=frozenset(),
        token_id="handle-1",
    )


def test_session_provider_ignores_requests_without_session_credentials(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(session_module, "get_session", boom)
    provider = SuperTokensSessionProvider(Settings())
    assert asyncio.run(provider.authenticate(_req([(b"authorization", b"Bearer x.y.z")]))) is None


def test_session_provider_rejects_expired_session(monkeypatch):
    async def expired(request, session_required=True):
        raise TryRefreshTokenError("expired")

    monkeypatch.setattr(session_module, "get_session", expired)
    provider = SuperTokensSessionProvider(Settings())
    with pytest.raises(AuthenticationError):
        asyncio.run(provider.authenticate(_req([(b"cookie", b"sAccessToken=abc")])))


def test_session_without_role_claim_has_no_roles(monkeypatch):
    class NoRoles(FakeSession):
        def get_access_token_payload(self):
            return {}

    async def fake_get_session(request, session_required=True):
        return NoRoles()

    monkeypatch.setattr(session_module, "get_session", fake_get_session)
    identity = asyncio.run(
        SuperTokensSessionProvider(Settings()).authenticate(
            _req([(b"cookie", b"sAccessToken=abc")])
        )
    )
    assert identity.roles == frozenset() and identity.tenant == "default"


# ---- provider ordering -------------------------------------------------------

class Stub:
    def __init__(self, name, result):
        self.name, self.result, self.calls = name, result, 0

    async def authenticate(self, request):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _identity(sub):
    return Identity(subject=sub, tenant="default", auth_method="session")


def test_bad_credentials_stop_the_chain_and_are_not_retried():
    bad = Stub("first", AuthenticationError("expired"))
    good = Stub("second", _identity("mallory"))
    app, _ = build()

    @app.get("/probe")
    async def probe(who: Identity = Depends(get_identity)):
        return {"sub": who.subject}

    app.dependency_overrides[get_providers] = lambda: [bad, good]
    r = TestClient(app).get("/probe")
    assert r.status_code == 401 and good.calls == 0


def test_first_matching_provider_wins_and_later_ones_are_skipped():
    first = Stub("first", _identity("alice"))
    second = Stub("second", _identity("bob"))
    app, _ = build()

    @app.get("/probe")
    async def probe(who: Identity = Depends(get_identity)):
        return {"sub": who.subject}

    app.dependency_overrides[get_providers] = lambda: [Stub("none", None), first, second]
    assert TestClient(app).get("/probe").json() == {"sub": "alice"} and second.calls == 0


# ---- self-service API keys (session-style caller) ----------------------------

@pytest.fixture()
def as_user():
    app, factory = build()

    def login(sub, roles, auth_method="session"):
        OWNER_ROLES[sub] = set(roles)
        app.dependency_overrides[get_identity] = lambda: Identity(
            subject=sub, tenant="default", auth_method=auth_method, roles=frozenset(roles)
        )

    return TestClient(app), factory, login


def _create(client, **over):
    body = {"name": "ci", "roles": ["analyst"], "scopes": ["model:small-*"], **over}
    return client.post("/v1/api-keys", json=body)


def test_user_can_create_key_with_own_roles_and_use_it(as_user):
    client, _, login = as_user
    login("alice", ["analyst", "viewer"])
    r = _create(client)
    assert r.status_code == 201
    created = r.json()
    assert created["api_key"].startswith("gk_") and created["roles"] == ["analyst"]
    assert created["expires_at"] is not None  # keys always expire

    client.app.dependency_overrides.pop(get_identity)
    ok = client.post(
        "/v1/inference",
        headers={"Authorization": f"Bearer {created['api_key']}"},
        json={"model": "small-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert ok.status_code == 200


def test_user_cannot_mint_a_key_with_roles_they_do_not_hold(as_user):
    client, _, login = as_user
    login("alice", ["viewer"])
    assert _create(client, roles=["admin"]).status_code == 403
    assert _create(client, roles=["viewer", "engineer"]).status_code == 403


@pytest.mark.parametrize(
    "over",
    [
        {"scopes": []},
        {"scopes": ["bogus"]},
        {"roles": ["root"]},
        {"roles": []},
        {"ttl_days": 0},
        {"ttl_days": 10_000},
        {"name": ""},
    ],
)
def test_invalid_key_requests_rejected(as_user, over):
    client, _, login = as_user
    login("alice", ["admin"])
    assert _create(client, **over).status_code == 422


def test_users_only_see_and_revoke_their_own_keys(as_user):
    client, factory, login = as_user
    login("alice", ["viewer"])
    alice_key = _create(client, roles=["viewer"]).json()
    login("bob", ["viewer"])
    assert client.get("/v1/api-keys").json() == []
    assert client.delete(f"/v1/api-keys/{alice_key['prefix']}").status_code == 404

    login("alice", ["viewer"])
    listing = client.get("/v1/api-keys").json()
    assert [k["prefix"] for k in listing] == [alice_key["prefix"]]
    assert "api_key" not in listing[0] and "secret_hash" not in listing[0]
    assert client.delete(f"/v1/api-keys/{alice_key['prefix']}").status_code == 204

    client.app.dependency_overrides.pop(get_identity)
    r = client.post(
        "/v1/inference",
        headers={"Authorization": f"Bearer {alice_key['api_key']}"},
        json={"model": "small-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401  # revoked


def test_keys_are_503_when_hmac_secret_not_configured(as_user):
    client, _, login = as_user
    client.app.dependency_overrides[app_settings] = lambda: Settings()
    login("alice", ["viewer"])
    assert _create(client, roles=["viewer"]).status_code == 503


def test_oauth2_identities_cannot_create_keys(as_user):
    client, _, login = as_user
    login("idp-user", ["analyst"], auth_method="oauth2")
    r = _create(client)
    assert r.status_code == 403 and "signed-in session" in r.json()["detail"]


def test_owner_roles_resolver_only_trusts_supertokens_sessions(monkeypatch):
    async def fake_list_roles(user_id, tenant_id="public"):
        return ["viewer"]

    monkeypatch.setattr(identity_service, "list_roles", fake_list_roles)
    assert asyncio.run(identity_service.owner_roles("u", "session")) == {"viewer"}
    assert asyncio.run(identity_service.owner_roles("u", "oauth2")) is None
    assert asyncio.run(identity_service.owner_roles("u", "api_key")) is None
