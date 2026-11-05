"""FastAPI dependencies: authenticate a request, then authorize each resource it touches.

Everything the audit layer needs is left on `request.state`:
  identity        Identity of the caller (set on successful authentication)
  auth_error      why authentication failed (string; never echoed to the client)
  decisions       list[RecordedDecision], one per authorization check
  inference_meta  dict describing an inference call (set by the inference route)
"""
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional

from fastapi import Depends, HTTPException, Request

from app.authentication.api_key import ApiKeyProvider
from app.authentication.base import AuthenticationError, AuthProvider, Identity
from app.authentication.oauth2 import OAuth2BearerProvider
from app.authentication.session import SuperTokensSessionProvider
from app.authorization.policies import get_engine
from app.config.database import SessionLocal
from app.config.settings import Settings, get_settings
from app.services import identity_service


@dataclass(frozen=True)
class RecordedDecision:
    resource: str
    action: str
    allowed: bool
    reason: str
    role: Optional[str]
    enforced: bool  # False when AUTH_ENFORCE=false (audit-only mode)


@lru_cache(maxsize=1)
def app_settings() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def get_providers() -> list[AuthProvider]:
    settings = app_settings()
    hmac_secret = (
        settings.api_key_hmac_secret.get_secret_value() if settings.api_key_hmac_secret else None
    )
    # Order matters only when a request carries several credential types: first match wins.
    return [
        SuperTokensSessionProvider(settings),
        ApiKeyProvider(SessionLocal, hmac_secret, identity_service.owner_roles),
        OAuth2BearerProvider(settings),
    ]


def decisions_of(request: Request) -> list[RecordedDecision]:
    if not hasattr(request.state, "decisions"):
        request.state.decisions = []
    return request.state.decisions


def record_decision(
    request: Request,
    resource: str,
    action: str,
    allowed: bool,
    reason: str,
    role: Optional[str] = None,
    enforced: bool = True,
) -> None:
    decisions_of(request).append(
        RecordedDecision(resource, action, allowed, reason, role, enforced)
    )


async def get_identity(
    request: Request, providers: list[AuthProvider] = Depends(get_providers)
) -> Identity:
    for provider in providers:
        try:
            identity = await provider.authenticate(request)
        except AuthenticationError as exc:
            # Credentials were presented but are bad: stop, do not try another provider.
            request.state.auth_error = f"{provider.name}: {exc}"
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        if identity is not None:
            request.state.identity = identity
            return identity

    request.state.auth_error = "no credentials presented"
    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authorize_all(
    request: Request,
    identity: Identity,
    checks: Iterable[tuple[str, str]],
    settings: Settings,
) -> None:
    """Evaluate every (resource, action) pair, record each, then raise 403 if any was denied.

    All checks are evaluated before raising so the audit trail shows the full request.
    """
    engine = get_engine()
    denied: list[str] = []
    for resource, action in checks:
        decision = engine.check(identity, resource, action)
        record_decision(
            request,
            resource,
            action,
            decision.allowed,
            decision.reason,
            decision.role,
            enforced=settings.auth_enforce,
        )
        if not decision.allowed:
            denied.append(f"{action} {resource}")
    if denied and settings.auth_enforce:
        raise HTTPException(status_code=403, detail="Not permitted: " + ", ".join(denied))


def require(resource: str, action: str):
    """Dependency factory for routes guarded by one fixed resource, e.g. require("admin:roles", "write")."""

    async def dependency(
        request: Request,
        identity: Identity = Depends(get_identity),
        settings: Settings = Depends(app_settings),
    ) -> Identity:
        authorize_all(request, identity, [(resource, action)], settings)
        return identity

    return dependency
