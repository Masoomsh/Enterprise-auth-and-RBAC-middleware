"""Authenticated reverse proxy in front of the stand-alone Semantic Cache & Model Router app.

The upstream application is used unmodified and must run with its own auth disabled
(AUTH_ENABLED=false) on a loopback/private address that only this gateway can reach. Everything
that reaches it has already been authenticated (get_identity) and authorized (Casbin) here.

What is enforced before forwarding: valid credential (401), requested model / tools / data
sources (403, 422 for malformed ids), `endpoint:reports` / `endpoint:feedback`.
What is enforced after the upstream answers: the model the upstream's router actually used
(`x-routed-model`) must be one the caller may invoke, otherwise the answer is withheld (403).
The upstream cannot see the caller, so its cache is shared across callers; see docs/design.md.
"""
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

from app.authentication.base import Identity
from app.authentication.dependencies import (
    app_settings,
    authorize_all,
    get_identity,
    require,
)
from app.authorization.permissions import concrete_resource
from app.config.settings import Settings

logger = logging.getLogger(__name__)

# Never forwarded: the upstream must not receive the caller's credentials.
_FORWARD_REQUEST_HEADERS = ("content-type", "x-cache-bypass", "x-request-id", "cache-control", "pragma")
_FORWARD_RESPONSE_HEADERS = (
    "x-cache", "x-routed-model", "x-query-complexity", "x-experiment-arm", "content-type",
)
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)  # local models can be slow


class SemanticUpstream:
    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_router(upstream: SemanticUpstream) -> tuple[APIRouter, Any]:
    router = APIRouter()

    def get_upstream() -> SemanticUpstream:
        return upstream

    async def forward(
        method: str, path: str, request: Request, up: SemanticUpstream, body: bytes | None = None
    ) -> httpx.Response:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST_HEADERS
        }
        try:
            return await up.client.request(method, path, content=body, headers=headers)
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Semantic cache service timed out")
        except httpx.HTTPError:
            logger.exception("Semantic cache upstream unreachable")
            raise HTTPException(status_code=502, detail="Semantic cache service unavailable")

    def relay(upstream_response: httpx.Response) -> Response:
        out = Response(content=upstream_response.content, status_code=upstream_response.status_code)
        for name in _FORWARD_RESPONSE_HEADERS:
            if name in upstream_response.headers:
                out.headers[name] = upstream_response.headers[name]
        return out

    @router.get("/health/ready")
    async def readiness(up: SemanticUpstream = Depends(get_upstream)) -> dict[str, str]:
        try:
            r = await up.client.get("/health/ready")
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Semantic cache service unavailable")
        if r.status_code != 200:
            raise HTTPException(status_code=503, detail="Service dependencies are not initialized")
        return {"status": "ready"}

    @router.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        identity: Identity = Depends(get_identity),
        settings: Settings = Depends(app_settings),
        up: SemanticUpstream = Depends(get_upstream),
    ) -> Response:
        raw = await request.body()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            model = payload.get("model")
            tools = payload.get("tools") or []
            sources = payload.get("data_sources") or []
            if not all(isinstance(x, str) for x in [*tools, *sources]) or not (
                model is None or isinstance(model, str)
            ):
                raise ValueError
            checks = [(concrete_resource("model", model), "invoke")] if model else []
            checks += [(concrete_resource("tool", t), "invoke") for t in sorted(set(tools))]
            checks += [(concrete_resource("datasource", d), "read") for d in sorted(set(sources))]
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid request body or identifier")

        meta: dict[str, Any] = {
            "model": model or "auto",
            "tools": sorted(set(tools)),
            "datasources": sorted(set(sources)),
            "message_count": len(payload.get("messages") or []),
        }
        request.state.inference_meta = meta
        authorize_all(request, identity, checks, settings)

        upstream_response = await forward("POST", "/v1/chat/completions", request, up, raw)
        if upstream_response.status_code == 200:
            routed = upstream_response.headers.get("x-routed-model")
            if routed:
                try:
                    routed_resource = concrete_resource("model", routed)
                except ValueError:
                    raise HTTPException(status_code=502, detail="Upstream returned an invalid model")
                # Raises 403 (and audits it) before any content is returned to the caller.
                authorize_all(request, identity, [(routed_resource, "invoke")], settings)
                meta["model"] = routed
            meta["model_requested"] = model
            meta["category"] = upstream_response.headers.get("x-query-complexity")
            meta["cache_hit"] = upstream_response.headers.get("x-cache") == "HIT"
            try:
                meta["usage"] = upstream_response.json().get("usage")
            except ValueError:
                pass
        return relay(upstream_response)

    reports = Depends(require("endpoint:reports", "read"))

    @router.get("/dashboard", dependencies=[reports])
    async def dashboard(request: Request, up: SemanticUpstream = Depends(get_upstream)) -> Response:
        return relay(await forward("GET", "/dashboard", request, up))

    @router.get("/dashboard/ui", dependencies=[reports])
    async def dashboard_ui(request: Request, up: SemanticUpstream = Depends(get_upstream)) -> Response:
        return relay(await forward("GET", "/dashboard/ui", request, up))

    @router.get("/reports/cost", dependencies=[reports])
    async def cost_report(request: Request, up: SemanticUpstream = Depends(get_upstream)) -> Response:
        return relay(await forward("GET", "/reports/cost", request, up))

    @router.post("/v1/feedback", dependencies=[Depends(require("endpoint:feedback", "write"))])
    async def feedback(request: Request, up: SemanticUpstream = Depends(get_upstream)) -> Response:
        return relay(await forward("POST", "/v1/feedback", request, up, await request.body()))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await upstream.close()

    router.get_upstream = get_upstream  # type: ignore[attr-defined]  (test override hook)
    return router, lifespan
