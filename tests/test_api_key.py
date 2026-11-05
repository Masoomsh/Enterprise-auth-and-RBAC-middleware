import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.authentication.api_key import ApiKeyProvider, create_api_key
from app.authentication.base import AuthenticationError
from app.config.database import Base
from app.models.api_key import ApiKey  # noqa: F401

SECRET = "test-server-secret"


async def OWNER_IS_ANALYST(subject, auth_method):
    return frozenset({"analyst"})


@pytest.fixture()
def factory():
    from sqlalchemy.pool import StaticPool  # one shared in-memory DB across threads

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def _auth(provider, key, header="authorization"):
    value = f"Bearer {key}" if header == "authorization" else key
    return asyncio.run(provider.authenticate(_request({header: value})))


def _make(factory, **kw):
    with factory() as db:
        return create_api_key(
            db,
            SECRET,
            owner_subject="user-1",
            name="ci",
            roles=["analyst"],
            scopes=["model:small-*"],
            **kw,
        )


def test_valid_key_yields_scoped_identity(factory):
    row, key = _make(factory)
    identity = _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), key)
    assert identity.subject == "user-1"
    assert identity.auth_method == "api_key"
    assert identity.roles == {"analyst"}
    assert identity.scopes == {"model:small-*"}
    assert identity.token_id == f"gk_{row.prefix}"


def test_x_api_key_header_is_accepted(factory):
    _, key = _make(factory)
    assert _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), key, header="x-api-key").subject == "user-1"


def test_non_key_request_is_ignored(factory):
    provider = ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST)
    assert asyncio.run(provider.authenticate(_request({"authorization": "Bearer a.b.c"}))) is None
    assert asyncio.run(provider.authenticate(_request({}))) is None


def test_wrong_secret_rejected(factory):
    row, _ = _make(factory)
    with pytest.raises(AuthenticationError):
        _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), f"gk_{row.prefix}.not-the-secret")


def test_unknown_prefix_rejected(factory):
    with pytest.raises(AuthenticationError):
        _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), "gk_deadbeef.whatever")


def test_hash_is_stored_not_secret(factory):
    row, key = _make(factory)
    assert key.split(".", 1)[1] not in row.secret_hash


def test_revoked_key_rejected(factory):
    row, key = _make(factory)
    with factory() as db:
        db.query(ApiKey).filter_by(id=row.id).update({"revoked": True})
        db.commit()
    with pytest.raises(AuthenticationError, match="revoked"):
        _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), key)


def test_expired_key_rejected(factory):
    _, key = _make(factory, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with pytest.raises(AuthenticationError, match="expired"):
        _auth(ApiKeyProvider(factory, SECRET, OWNER_IS_ANALYST), key)


def test_key_signed_with_other_server_secret_rejected(factory):
    _, key = _make(factory)
    with pytest.raises(AuthenticationError):
        _auth(ApiKeyProvider(factory, "different-secret", OWNER_IS_ANALYST), key)


def test_key_without_scopes_cannot_be_created(factory):
    with factory() as db, pytest.raises(ValueError):
        create_api_key(db, SECRET, owner_subject="u", name="n", roles=["viewer"], scopes=[])


# ---- owner roles are re-checked on every use -----------------------------------

def _use(factory, key, resolver):
    return _auth(ApiKeyProvider(factory, SECRET, resolver), key)


def _make_roles(factory, roles):
    with factory() as db:
        return create_api_key(
            db, SECRET, owner_subject="user-1", name="ci", roles=roles, scopes=["*"]
        )[1]


def test_owner_losing_all_key_roles_disables_the_key(factory):
    key = _make_roles(factory, ["analyst"])

    async def none_left(subject, method):
        return frozenset()

    with pytest.raises(AuthenticationError, match="no longer holds"):
        _use(factory, key, none_left)


def test_owner_losing_one_role_narrows_the_key(factory):
    key = _make_roles(factory, ["engineer", "viewer"])

    async def demoted(subject, method):
        return frozenset({"viewer"})

    assert _use(factory, key, demoted).roles == {"viewer"}


def test_owner_gaining_roles_does_not_widen_the_key(factory):
    key = _make_roles(factory, ["viewer"])

    async def promoted(subject, method):
        return frozenset({"viewer", "admin"})

    assert _use(factory, key, promoted).roles == {"viewer"}


def test_unverifiable_owner_rejects_the_key(factory):
    key = _make_roles(factory, ["analyst"])

    async def unknown(subject, method):
        return None

    with pytest.raises(AuthenticationError, match="cannot be verified"):
        _use(factory, key, unknown)


def test_role_lookup_failure_fails_closed(factory):
    key = _make_roles(factory, ["analyst"])

    async def down(subject, method):
        raise ConnectionError("supertokens unreachable")

    with pytest.raises(AuthenticationError, match="Could not verify"):
        _use(factory, key, down)


def test_resolver_receives_owner_and_auth_method(factory):
    key = _make_roles(factory, ["analyst"])
    seen = []

    async def spy(subject, method):
        seen.append((subject, method))
        return frozenset({"analyst"})

    _use(factory, key, spy)
    assert seen == [("user-1", "session")]


def test_invalid_key_never_reaches_the_resolver(factory):
    row, _ = _make_roles_row(factory)

    async def boom(subject, method):
        raise AssertionError("resolver must not run for invalid keys")

    with pytest.raises(AuthenticationError, match="Invalid API key"):
        _use(factory, f"gk_{row.prefix}.wrong", boom)


def _make_roles_row(factory):
    with factory() as db:
        return create_api_key(
            db, SECRET, owner_subject="user-1", name="ci", roles=["analyst"], scopes=["*"]
        )
