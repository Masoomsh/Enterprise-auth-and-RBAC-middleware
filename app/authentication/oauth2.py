"""OAuth2/OIDC bearer-token provider: verifies IdP-issued JWTs against the IdP's JWKS."""
import asyncio
import json
from typing import Any, Optional

import jwt
from fastapi import Request
from jwt import PyJWKClient

from app.authentication.api_key import KEY_PREFIX
from app.authentication.base import AuthenticationError, Identity
from app.config.settings import Settings

# Asymmetric only. Never add "none" or HS* here: an attacker controls the token header.
ALLOWED_ALGORITHMS = ["RS256", "ES256"]


class OAuth2BearerProvider:
    name = "oauth2"

    def __init__(self, settings: Settings):
        self._issuer = settings.oauth2_issuer
        self._audience = settings.oauth2_audience
        self._group_claim = settings.oauth2_group_claim
        self._role_map: dict[str, str] = json.loads(settings.oauth2_role_map or "{}")
        self._default_tenant = settings.default_tenant
        self._jwks = (
            PyJWKClient(settings.oauth2_jwks_uri, cache_keys=True, lifespan=3600)
            if settings.oauth2_jwks_uri
            else None
        )
        self.enabled = bool(self._jwks and self._issuer and self._audience)

    def _signing_key(self, token: str) -> Any:
        # Blocking network call on cache miss; always invoked via asyncio.to_thread.
        return self._jwks.get_signing_key_from_jwt(token).key

    async def authenticate(self, request: Request) -> Optional[Identity]:
        if not self.enabled:
            return None
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or token.startswith(KEY_PREFIX):
            return None
        return await asyncio.to_thread(self._verify, token)

    def _verify(self, token: str) -> Identity:
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALLOWED_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"Invalid bearer token: {type(exc).__name__}") from exc

        groups = claims.get(self._group_claim, [])
        if isinstance(groups, str):
            groups = [groups]
        # Unmapped groups grant nothing: roles only come from the explicit mapping.
        roles = frozenset(self._role_map[g] for g in groups if g in self._role_map)

        scope = claims.get("scope", claims.get("scp", []))
        scopes = frozenset(scope.split() if isinstance(scope, str) else scope)

        return Identity(
            subject=str(claims["sub"]),
            tenant=str(claims.get("tenant", self._default_tenant)),
            auth_method="oauth2",
            roles=roles,
            scopes=scopes,
            token_id=claims.get("jti"),
        )
