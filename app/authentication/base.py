"""Contracts shared by every authentication provider.

A provider turns a request into an `Identity` (or None if it has nothing to say about the
request). It must raise `AuthenticationError` when credentials ARE presented but invalid, so a
bad token is never silently retried against another provider.
"""
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Protocol

from fastapi import Request


class AuthenticationError(Exception):
    """Credentials were presented but are invalid, expired, revoked or out of scope."""


@dataclass(frozen=True)
class Identity:
    subject: str
    tenant: str
    auth_method: str  # "session" | "oauth2" | "api_key"
    roles: FrozenSet[str] = field(default_factory=frozenset)
    # Token/key scopes, as resource patterns such as "model:small-*". Empty means
    # "no narrowing beyond the roles" only for sessions; API keys must carry explicit scopes.
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    token_id: Optional[str] = None  # jti or API key prefix; never a secret


class AuthProvider(Protocol):
    name: str

    async def authenticate(self, request: Request) -> Optional[Identity]:
        """Return an Identity, None if this provider does not apply, or raise AuthenticationError."""
        ...
