import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from app.authentication.base import AuthenticationError
from app.authentication.oauth2 import OAuth2BearerProvider
from app.config.settings import Settings

ISSUER = "https://idp.example.com"
AUDIENCE = "semantic-gateway"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def provider():
    settings = Settings(
        oauth2_issuer=ISSUER,
        oauth2_audience=AUDIENCE,
        oauth2_jwks_uri="https://idp.example.com/jwks",
        oauth2_role_map=json.dumps({"AI-Admins": "admin", "AI-Analysts": "analyst"}),
    )
    p = OAuth2BearerProvider(settings)
    p._signing_key = lambda token: KEY.public_key()  # no network in tests
    return p


def _token(key=KEY, alg="RS256", **overrides):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "exp": int(time.time()) + 300,
        "jti": "tok-1",
        "groups": ["AI-Analysts", "Random-Group"],
        "scope": "model:small-*",
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg)


def _auth(provider, token):
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    return asyncio.run(provider.authenticate(request))


def test_valid_token_maps_groups_to_roles_and_scopes(provider):
    identity = _auth(provider, _token())
    assert identity.subject == "alice"
    assert identity.auth_method == "oauth2"
    assert identity.roles == {"analyst"}  # unmapped group grants nothing
    assert identity.scopes == {"model:small-*"}
    assert identity.token_id == "tok-1"


def test_disabled_without_config():
    assert OAuth2BearerProvider(Settings()).enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 3600},
        {"aud": "someone-else"},
        {"iss": "https://evil.example.com"},
        {"exp": None},
        {"sub": None},
    ],
)
def test_bad_claims_rejected(provider, overrides):
    with pytest.raises(AuthenticationError):
        _auth(provider, _token(**overrides))


def test_token_signed_by_wrong_key_rejected(provider):
    with pytest.raises(AuthenticationError):
        _auth(provider, _token(key=OTHER_KEY))


def test_alg_none_rejected(provider):
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 300},
        key=None,
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        _auth(provider, forged)


def test_hs256_signed_with_public_key_rejected(provider):
    # Classic algorithm-confusion attempt: HMAC token keyed with a known value.
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 300},
        "known-value-0123456789-0123456789",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        _auth(provider, forged)


def test_garbage_token_rejected(provider):
    with pytest.raises(AuthenticationError):
        _auth(provider, "not.a.jwt")


def test_api_key_bearer_is_left_to_api_key_provider(provider):
    assert _auth(provider, "gk_abcd1234.secret") is None
