"""Builds and persists one audit row per audited request.

Decision semantics (one row per request):
  deny   authentication failed, or any authorization check was denied
         (in audit-only mode the request may still have succeeded; see details.enforced)
  allow  every authorization check passed, or the caller was authenticated and the route
         has no RBAC check, and the response was not an error
  error  authenticated caller, no authorization decision, error response (e.g. 422)

Never put prompts, secrets or raw tokens in a row.
"""
import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

logger = logging.getLogger("audit")

AUDITED_PREFIXES = ("/v1/", "/admin/", "/dashboard", "/reports/")


def should_audit(path: str, method: str) -> bool:
    return method != "OPTIONS" and path.startswith(AUDITED_PREFIXES)


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    return value if value is None or len(value) <= limit else value[:limit]


def build_event(
    scope: dict, status_code: int, latency_ms: int, request_id: str
) -> dict[str, Any]:
    state: dict = scope.get("state", {})
    identity = state.get("identity")
    decisions = state.get("decisions", [])
    meta = state.get("inference_meta")
    auth_error = state.get("auth_error")

    denied = [d for d in decisions if not d.allowed]
    if identity is None:
        decision, reason = "deny", auth_error or "unauthenticated"
    elif denied:
        decision, reason = "deny", denied[0].reason
    elif decisions:
        decision, reason = "allow", decisions[0].reason
    elif status_code < 400:
        decision, reason = "allow", "authenticated; route has no rbac check"
    else:
        decision, reason = "error", f"http {status_code} before authorization"

    route = scope.get("route")
    template = getattr(route, "path", None) or scope["path"]
    action = "inference.invoke" if meta else f"{scope['method']} {template}"

    if denied:  # name what was refused, even when the request also named a model
        resource = denied[0].resource
    elif meta:
        resource = f"model:{meta['model']}"
    elif decisions:
        resource = decisions[0].resource
    else:
        resource = template

    details = {
        "method": scope["method"],
        "path": scope["path"],
        "decisions": [asdict(d) for d in decisions],
        "enforced": all(d.enforced for d in decisions) if decisions else True,
    }
    if meta:
        details["inference"] = meta

    client = scope.get("client")
    return {
        "request_id": request_id,
        "subject": _clip(identity.subject, 255) if identity else None,
        "tenant": _clip(identity.tenant, 100) if identity else None,
        "auth_method": identity.auth_method if identity else "none",
        "token_id": _clip(identity.token_id, 128) if identity else None,
        "action": _clip(action, 100),
        "resource": _clip(resource, 255),
        "decision": decision,
        "reason": _clip(reason, 255),
        "status_code": status_code,
        # Peer address only. Behind a proxy, run uvicorn with --proxy-headers and a
        # restricted --forwarded-allow-ips rather than trusting X-Forwarded-For here.
        "ip_address": client[0] if client else None,
        "latency_ms": latency_ms,
        "details": json.dumps(details, default=str),
    }


def _insert(factory: Callable[[], Session], fields: dict[str, Any]) -> None:
    with factory() as db:
        db.add(AuditLog(**fields))
        db.commit()


async def write_event(factory: Callable[[], Session], fields: dict[str, Any]) -> None:
    """Fail-open for the caller (the response is already sent), but never silently:
    on failure the full event is emitted to the 'audit' logger for the SIEM to collect."""
    try:
        await asyncio.to_thread(_insert, factory, fields)
    except Exception:
        logger.exception("AUDIT_WRITE_FAILED event=%s", json.dumps(fields, default=str))
