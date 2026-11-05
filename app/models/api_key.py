from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.config.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiKey(Base):
    """Machine credential. Only an HMAC of the secret is stored; it is shown once at creation."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    prefix = Column(String(16), unique=True, nullable=False, index=True)
    secret_hash = Column(String(128), nullable=False)
    owner_subject = Column(String(255), nullable=False, index=True)
    # Which identity system owns `owner_subject`; decides where the owner's CURRENT roles
    # are looked up every time the key is used.
    owner_auth_method = Column(String(32), nullable=False, default="session")
    tenant = Column(String(100), nullable=False, default="default")
    name = Column(String(100), nullable=False)
    # Space-separated scopes, e.g. "model:small-* tool:search". Role permissions are
    # intersected with these, so a key can never exceed its owner's roles.
    scopes = Column(Text, nullable=False, default="")
    # Space-separated gateway roles. This is a CEILING: at use time the effective roles are
    # these intersected with the owner's current roles.
    roles = Column(Text, nullable=False, default="")
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
