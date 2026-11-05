"""API-key provider. Key format: `gk_<prefix>.<secret>`; only HMAC-SHA256(secret) is stored."""
import asyncio
import dataclasses
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Optional, Tuple

from fastapi import Request
from sqlalchemy.orm import Session

from app.authentication.base import AuthenticationError, Identity
from app.models.api_key import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "gk_"
_DUMMY_HASH = "0" * 64

# (owner_subject, owner_auth_method) -> the owner's CURRENT roles, or None if they cannot be
# determined (which makes the key unusable: fail closed).
OwnerRoleResolver = Callable[[str, str], Awaitable[Optional[Iterable[str]]]]


def _hash_secret(server_secret: str, secret: str) -> str:
    return hmac.new(server_secret.encode(), secret.encode(), hashlib.sha256).hexdigest()


def _aware(value: datetime) -> datetime:
    # SQLite drops tzinfo on read; everything we store is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_api_key(
    db: Session,
    server_secret: str,
    *,
    owner_subject: str,
    name: str,
    roles: Iterable[str],
    scopes: Iterable[str],
    tenant: str = "default",
    owner_auth_method: str = "session",
    expires_at: Optional[datetime] = None,
) -> Tuple[ApiKey, str]:
    """Returns (row, full_key). The full key is shown once and cannot be recovered.

    `roles` is a ceiling: when the key is used, its effective roles are these intersected with
    the owner's current roles."""
    scopes = sorted(set(scopes))
    roles = sorted(set(roles))
    if not scopes:
        raise ValueError("API keys must have at least one explicit scope.")
    if not roles:
        raise ValueError("API keys must have at least one role.")
    if not server_secret:
        raise RuntimeError("API_KEY_HMAC_SECRET is not configured.")

    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    row = ApiKey(
        prefix=prefix,
        secret_hash=_hash_secret(server_secret, secret),
        owner_subject=owner_subject,
        owner_auth_method=owner_auth_method,
        tenant=tenant,
        name=name,
        scopes=" ".join(scopes),
        roles=" ".join(roles),
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, f"{KEY_PREFIX}{prefix}.{secret}"


def extract_api_key(request: Request) -> Optional[str]:
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip().startswith(KEY_PREFIX):
        return token.strip()
    return None


class ApiKeyProvider:
    name = "api_key"

    def __init__(
        self,
        session_factory: Callable[[], Session],
        server_secret: Optional[str],
        owner_roles: OwnerRoleResolver,  # required on purpose: no unchecked default
    ):
        self._session_factory = session_factory
        self._server_secret = server_secret
        self._owner_roles = owner_roles

    async def authenticate(self, request: Request) -> Optional[Identity]:
        raw = extract_api_key(request)
        if raw is None:
            return None
        identity, owner_auth_method = await asyncio.to_thread(self._verify, raw)
        return await self._narrow_to_owner_roles(identity, owner_auth_method)

    async def _narrow_to_owner_roles(self, identity: Identity, owner_auth_method: str) -> Identity:
        """Effective roles = key roles ∩ owner's current roles. Any doubt rejects the key."""
        try:
            current = await self._owner_roles(identity.subject, owner_auth_method)
        except Exception as exc:  # identity backend down, user deleted, etc.
            logger.exception("Could not look up roles for API key owner")
            raise AuthenticationError("Could not verify the key owner's roles.") from exc
        if current is None:
            raise AuthenticationError("The key owner's roles cannot be verified.")
        effective = identity.roles & frozenset(current)
        if not effective:
            raise AuthenticationError("The key owner no longer holds any of the key's roles.")
        return dataclasses.replace(identity, roles=effective)

    def _verify(self, raw: str) -> Tuple[Identity, str]:
        if not self._server_secret:
            raise AuthenticationError("API keys are not enabled.")
        if not raw.startswith(KEY_PREFIX) or "." not in raw:
            raise AuthenticationError("Malformed API key.")
        prefix, _, secret = raw[len(KEY_PREFIX):].partition(".")

        with self._session_factory() as db:
            row = db.query(ApiKey).filter(ApiKey.prefix == prefix).one_or_none()
            # Always hash and compare, so unknown prefixes cost the same as wrong secrets.
            candidate = _hash_secret(self._server_secret, secret)
            expected = row.secret_hash if row else _DUMMY_HASH
            matches = hmac.compare_digest(candidate, expected)
            if row is None or not matches:
                raise AuthenticationError("Invalid API key.")
            if row.revoked:
                raise AuthenticationError("API key has been revoked.")
            if row.expires_at is not None and _aware(row.expires_at) <= datetime.now(timezone.utc):
                raise AuthenticationError("API key has expired.")

            row.last_used_at = datetime.now(timezone.utc)
            identity = Identity(
                subject=row.owner_subject,
                tenant=row.tenant,
                auth_method="api_key",
                roles=frozenset(row.roles.split()),
                scopes=frozenset(row.scopes.split()),
                token_id=f"{KEY_PREFIX}{row.prefix}",
            )
            owner_auth_method = row.owner_auth_method
            db.commit()
            return identity, owner_auth_method
