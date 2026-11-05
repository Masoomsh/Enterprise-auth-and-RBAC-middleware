"""Pure-ASGI audit middleware: one row per request under /v1 and /admin, including 401/403/5xx."""
import time
import uuid
from typing import Callable

from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.audit_service import build_event, should_audit, write_event


class AuditMiddleware:
    def __init__(self, app: ASGIApp, session_factory: Callable[[], Session]):
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not should_audit(scope["path"], scope["method"]):
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = {"code": 500}  # stays 500 if the app raises before responding

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            fields = build_event(
                scope, status["code"], int((time.perf_counter() - started) * 1000), request_id
            )
            await write_event(self.session_factory, fields)
