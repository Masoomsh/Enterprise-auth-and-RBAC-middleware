"""Self-service API keys. Mounted at /v1/api-keys because SuperTokens owns /auth/*."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.authentication.api_key import create_api_key
from app.authentication.base import Identity
from app.authentication.dependencies import app_settings, get_identity, record_decision
from app.authorization.permissions import is_valid_scope
from app.config.database import get_db
from app.config.settings import Settings
from app.models.api_key import ApiKey
from app.services.identity_service import GATEWAY_ROLES

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365


class ApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    roles: list[str] = Field(min_length=1, max_length=len(GATEWAY_ROLES))
    scopes: list[str] = Field(min_length=1, max_length=50)
    ttl_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1, le=MAX_TTL_DAYS)

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, roles):
        unknown = set(roles) - set(GATEWAY_ROLES)
        if unknown:
            raise ValueError(f"unknown roles: {sorted(unknown)}")
        return roles

    @field_validator("scopes")
    @classmethod
    def _valid_scopes(cls, scopes):
        bad = [s for s in scopes if not is_valid_scope(s)]
        if bad:
            raise ValueError(f"invalid scopes: {bad}")
        return scopes


def hmac_secret_or_503(settings: Settings) -> str:
    if not settings.api_key_hmac_secret:
        raise HTTPException(status_code=503, detail="API keys are not enabled")
    return settings.api_key_hmac_secret.get_secret_value()


def key_summary(row: ApiKey) -> dict:
    return {
        "prefix": row.prefix,
        "name": row.name,
        "roles": row.roles.split(),
        "scopes": row.scopes.split(),
        "revoked": row.revoked,
        "expires_at": row.expires_at,
        "last_used_at": row.last_used_at,
    }


@router.post("", status_code=201)
def create_my_key(
    body: ApiKeyRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(app_settings),
    db: Session = Depends(get_db),
):
    secret = hmac_secret_or_503(settings)
    # Keys can only be created from a SuperTokens session: that is the one identity whose
    # current roles we can re-check on every use of the key. This also stops a key from
    # minting more keys (persistence after the owner's access is revoked).
    if identity.auth_method != "session":
        record_decision(
            request, "admin:api-keys", "write", False,
            f"{identity.auth_method} identities cannot create api keys",
        )
        raise HTTPException(
            status_code=403, detail="API keys can only be created from a signed-in session"
        )
    extra = set(body.roles) - identity.roles
    if extra:
        record_decision(request, "admin:api-keys", "write", False, f"roles not held: {sorted(extra)}")
        raise HTTPException(status_code=403, detail=f"You do not hold roles: {sorted(extra)}")

    record_decision(request, "admin:api-keys", "write", True, "self-service key creation")
    row, full_key = create_api_key(
        db,
        secret,
        owner_subject=identity.subject,
        name=body.name,
        roles=body.roles,
        scopes=body.scopes,
        tenant=identity.tenant,
        owner_auth_method=identity.auth_method,
        expires_at=datetime.now(timezone.utc) + timedelta(days=body.ttl_days),
    )
    # The only time the full key is ever returned.
    return {**key_summary(row), "api_key": full_key}


@router.get("")
def list_my_keys(identity: Identity = Depends(get_identity), db: Session = Depends(get_db)):
    rows = db.query(ApiKey).filter(ApiKey.owner_subject == identity.subject).all()
    return [key_summary(r) for r in rows]


@router.delete("/{prefix}", status_code=204)
def revoke_my_key(
    prefix: str,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
):
    row: Optional[ApiKey] = (
        db.query(ApiKey)
        .filter(ApiKey.prefix == prefix, ApiKey.owner_subject == identity.subject)
        .one_or_none()
    )
    if row is None:  # also covers other people's keys: do not reveal they exist
        raise HTTPException(status_code=404, detail="Not found")
    row.revoked = True
    db.commit()
