from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, event

from app.config.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    """One row per authentication/authorization decision. Insert-only by convention.

    Never store prompts, secrets or raw tokens here; store hashes or lengths instead.
    """

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    request_id = Column(String(64), nullable=False, index=True)
    # SuperTokens user id, API key owner, or IdP `sub`. NULL when unauthenticated.
    subject = Column(String(255), nullable=True, index=True)
    tenant = Column(String(100), nullable=True, index=True)
    auth_method = Column(String(32), nullable=True)  # session | oauth2 | api_key | none
    token_id = Column(String(128), nullable=True)  # jti or API key prefix, never the secret
    action = Column(String(100), nullable=False, index=True)  # e.g. inference.invoke
    resource = Column(String(255), nullable=False)  # e.g. model:gpt-4o
    decision = Column(String(8), nullable=False, index=True)  # allow | deny
    reason = Column(String(255), nullable=True)
    status_code = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)  # JSON: tools, data sources, token counts


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_rows_are_immutable(mapper, connection, target):
    # Defence in depth only: bulk queries bypass ORM events. The real control is a database
    # role that has INSERT and SELECT, but not UPDATE or DELETE, on this table.
    raise RuntimeError("audit_events rows are insert-only")
