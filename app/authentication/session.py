"""SuperTokens session provider (browser/UI logins, including federated corporate IdP logins)."""
from typing import Optional

from fastapi import Request
from supertokens_python.recipe.session.asyncio import get_session
from supertokens_python.recipe.session.exceptions import (
    TryRefreshTokenError,
    UnauthorisedError,
)

from app.authentication.base import AuthenticationError, Identity
from app.config.settings import Settings


class SuperTokensSessionProvider:
    name = "session"

    def __init__(self, settings: Settings):
        self._default_tenant = settings.default_tenant

    async def authenticate(self, request: Request) -> Optional[Identity]:
        # Only act when a SuperTokens session credential is present, so API keys and IdP
        # bearer tokens are left to their own providers.
        if "sAccessToken" not in request.cookies and request.headers.get("st-auth-mode") is None:
            return None
        try:
            session = await get_session(request, session_required=False)
        except (TryRefreshTokenError, UnauthorisedError) as exc:
            raise AuthenticationError(f"Invalid session: {type(exc).__name__}") from exc
        if session is None:
            return None

        payload = session.get_access_token_payload()
        roles = payload.get("st-role", {}).get("v", [])
        return Identity(
            subject=session.get_user_id(),
            tenant=str(payload.get("tenant", self._default_tenant)),
            auth_method="session",
            roles=frozenset(roles),
            scopes=frozenset(),
            token_id=session.get_handle(),
        )
