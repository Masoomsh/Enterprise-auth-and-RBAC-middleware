"""Semantic cache and model-routing endpoints, mounted behind the enterprise auth layer.

Every route here depends on `get_identity` (SuperTokens session, API key or IdP bearer token),
so an unauthenticated request is rejected with 401 before any cache, embedding or model work
happens. Casbin RBAC then authorizes each resource the call touches: the model, each tool and
data source, and `endpoint:reports` / `endpoint:feedback` for the non-inference routes.

Audit rows come from `AuditMiddleware`; this module only fills `request.state.inference_meta`
(metadata only, never prompts or completions).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.authentication.base import Identity
from app.authentication.dependencies import (
    app_settings,
    authorize_all,
    get_identity,
    require,
)
from app.authorization.permissions import concrete_resource
from app.authorization.policies import get_engine
from app.config.settings import Settings as AuthSettings
from semantic_router.analytics import AnalyticsStore
from semantic_router.cache import CacheEntry, PineconeSemanticCache
from semantic_router.config import Settings
from semantic_router.providers import OllamaClient
from semantic_router.routing import ModelRouter, RoutingRuleEngine

logger = logging.getLogger(__name__)
_system_trust_configured = False


@dataclass
class Runtime:
    settings: Settings
    cache: Any
    ollama: Any
    analytics: AnalyticsStore
    router: ModelRouter
    embedding_dimension: int | None = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # Names of tools / data sources this call intends to use; each is checked against RBAC.
    tools: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    request_id: str
    quality_score: float = Field(ge=1, le=5)


class SemanticGateway:
    """Owns the semantic-cache runtime and exposes it as an authenticated APIRouter."""

    def __init__(
        self,
        settings: Settings | None = None,
        cache: Any | None = None,
        ollama: Any | None = None,
        analytics: AnalyticsStore | None = None,
    ) -> None:
        self.config = settings or Settings.from_env()
        self.config.validate()
        self._runtime: Runtime | None = None
        self.ready = False
        if cache is not None or ollama is not None or analytics is not None:
            if cache is None or ollama is None or analytics is None:
                raise ValueError("Inject cache, Ollama client, and analytics store together")
            self._runtime = _make_runtime(self.config, cache, ollama, analytics)
        self.router = self._build_router()

    # ---- runtime lifecycle ---------------------------------------------------------------

    def get_runtime(self) -> Runtime:
        if self._runtime is None:
            config = self.config
            if not config.pinecone_api_key:
                raise HTTPException(status_code=503, detail="PINECONE_API_KEY is not configured")
            _configure_system_trust()
            from pinecone import Pinecone

            pinecone = Pinecone(api_key=config.pinecone_api_key)
            description = pinecone.describe_index(config.pinecone_index)
            index = pinecone.Index(config.pinecone_index)
            cache_store = PineconeSemanticCache(
                index=index,
                namespace=config.pinecone_namespace,
                similarity_threshold=config.similarity_threshold,
                ttl_seconds=config.cache_ttl_seconds,
                max_response_chars=config.cache_max_response_chars,
                vector_field=_dense_vector_field(description),
            )
            self._runtime = _make_runtime(
                config, cache_store, OllamaClient(config.ollama_base_url),
                AnalyticsStore(config.metrics_db_path),
                embedding_dimension=_field(description, "dimension"),
            )
        return self._runtime

    async def warm_up(self) -> None:
        """Check Pinecone and Ollama are reachable and dimensionally compatible."""
        active = self.get_runtime()
        embedding = await active.ollama.embed(
            self.config.embedding_model, "FastAPI semantic cache readiness check"
        )
        index_dimension = active.embedding_dimension
        if index_dimension is not None and len(embedding) != index_dimension:
            raise RuntimeError(
                f"Ollama embedding dimension {len(embedding)} does not match "
                f"Pinecone index dimension {index_dimension}"
            )
        self.ready = True

    @asynccontextmanager
    async def lifespan(self, _: FastAPI):
        # A cache backend outage must not take the authentication layer down with it:
        # log it, report 503 on /health/ready, and retry lazily on the first request.
        try:
            await self.warm_up()
        except Exception:
            logger.exception("Semantic cache backends are not ready; continuing without them")
        try:
            yield
        finally:
            active = self._runtime
            if active is not None:
                close = getattr(active.ollama, "close", None)
                if close is not None:
                    await close()
                active.analytics.close()

    # ---- routes --------------------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        config = self.config
        get_runtime = self.get_runtime

        @router.get("/health/ready")
        async def readiness() -> dict[str, str]:
            if not self.ready:
                raise HTTPException(status_code=503, detail="Service dependencies are not initialized")
            return {"status": "ready"}

        @router.post("/v1/chat/completions")
        async def chat_completions(
            payload: ChatRequest,
            request: Request,
            response: Response,
            identity: Identity = Depends(get_identity),
            auth_settings: AuthSettings = Depends(app_settings),
            x_cache_bypass: str | None = Header(default=None),
            x_request_id: str | None = Header(default=None),
            pragma: str | None = Header(default=None),
        ) -> Any:
            request_id = _safe_request_id(x_request_id)
            tools = sorted(set(payload.tools))
            sources = sorted(set(payload.data_sources))
            try:
                checks = [(concrete_resource("model", payload.model), "invoke")] if payload.model else []
                checks += [(concrete_resource("tool", t), "invoke") for t in tools]
                checks += [(concrete_resource("datasource", d), "read") for d in sources]
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid model, tool or data source identifier")

            # Metadata only: the audit layer never sees prompts or completions.
            meta: dict[str, Any] = {
                "model": payload.model or "auto",
                "tools": tools,
                "datasources": sources,
                "message_count": len(payload.messages),
                "prompt_chars": sum(len(m.content) for m in payload.messages),
                "request_id": request_id,
            }
            request.state.inference_meta = meta
            authorize_all(request, identity, checks, auth_settings)

            active = get_runtime()
            if payload.stream:
                raise HTTPException(status_code=400, detail="Streaming responses are not supported")
            if any(m.role not in {"system", "user", "assistant"} for m in payload.messages):
                raise HTTPException(status_code=422, detail="Messages must use system, user, or assistant roles")

            messages = [m.model_dump() for m in payload.messages]
            prompt = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            user_text = "\n".join(m.content for m in payload.messages if m.role == "user")

            def may_use(model: str) -> bool:
                try:
                    resource = concrete_resource("model", model)
                except ValueError:
                    return False
                return get_engine().check(identity, resource, "invoke").allowed

            try:
                decision = active.router.route(
                    user_text,
                    request_id,
                    config.ab_test_enabled,
                    config.ab_test_treatment_percent,
                    may_use if auth_settings.auth_enforce else None,
                )
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403, detail="No permitted model available for this identity"
                ) from exc
            # Authorize (and so audit) the model the router actually chose.
            try:
                routed = concrete_resource("model", decision.model)
            except ValueError:
                logger.error("Routed model %r is not a valid resource identifier", decision.model)
                raise HTTPException(status_code=500, detail="Routing configuration error")
            authorize_all(request, identity, [(routed, "invoke")], auth_settings)
            meta.update(model_requested=payload.model, model=decision.model, category=decision.category)

            bypass = _should_bypass_cache(x_cache_bypass, request.headers.get("cache-control"), pragma)
            options: dict[str, Any] = {}
            if payload.temperature is not None:
                options["temperature"] = payload.temperature
            if payload.top_p is not None:
                options["top_p"] = payload.top_p
            if payload.max_tokens is not None:
                options["num_predict"] = payload.max_tokens
            # Cache entries are partitioned by tenant and roles so one caller's answer is never
            # served to a caller with different entitlements.
            access = f"{identity.tenant}|{','.join(sorted(identity.roles))}"
            scope_id = _scope_id(messages, decision.category, decision.model, options, access)
            vector: list[float] | None = None
            entry: CacheEntry | None = None
            if not bypass:
                try:
                    vector = await active.ollama.embed(config.embedding_model, prompt)
                    entry = await active.cache.lookup(vector, scope_id)
                except Exception as exc:
                    logger.exception("Semantic cache lookup failed for request %s", request_id)
                    raise HTTPException(status_code=502, detail="Semantic cache lookup failed") from exc

            cache_hit = entry is not None
            model = decision.model
            if entry is None:
                try:
                    result = await active.ollama.chat(model, messages, options)
                    entry = CacheEntry(
                        result["message"]["content"],
                        int(result.get("prompt_eval_count", 0)),
                        int(result.get("eval_count", 0)),
                    )
                    if not bypass and vector is not None:
                        await active.cache.store(vector, scope_id, decision.category, model, entry)
                except Exception as exc:
                    logger.exception("Model request failed for request %s", request_id)
                    raise HTTPException(status_code=502, detail="Model request failed") from exc

            input_rate, output_rate = active.router.price(model, config.model_prices)
            baseline_input_rate, baseline_output_rate = active.router.price(
                config.baseline_model, config.model_prices
            )
            actual_cost = 0.0 if cache_hit else _cost(
                entry.prompt_tokens, entry.completion_tokens, input_rate, output_rate
            )
            baseline_cost = _cost(
                entry.prompt_tokens, entry.completion_tokens,
                baseline_input_rate, baseline_output_rate,
            )
            try:
                active.analytics.record_request({
                    "request_id": request_id,
                    "arm": decision.arm,
                    "category": decision.category,
                    "model": model,
                    "cache_hit": int(cache_hit),
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                    "actual_cost": actual_cost,
                    "baseline_cost": baseline_cost,
                })
            except Exception as exc:
                logger.exception("Could not record metrics for request %s", request_id)
                raise HTTPException(status_code=500, detail="Could not record request metrics") from exc

            meta.update(
                cache_hit=cache_hit,
                usage={
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                },
                actual_cost=actual_cost,
            )
            response.headers["x-cache"] = "HIT" if cache_hit else ("BYPASS" if bypass else "MISS")
            response.headers["x-request-id"] = request_id
            response.headers["x-routed-model"] = model
            response.headers["x-query-complexity"] = decision.category
            response.headers["x-experiment-arm"] = decision.arm
            return {
                "id": request_id,
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": entry.content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                    "total_tokens": entry.prompt_tokens + entry.completion_tokens,
                },
            }

        reports = Depends(require("endpoint:reports", "read"))

        @router.get("/dashboard", dependencies=[reports])
        async def dashboard() -> dict[str, Any]:
            return get_runtime().analytics.dashboard()

        @router.get("/dashboard/ui", response_class=HTMLResponse, dependencies=[reports])
        async def dashboard_ui() -> str:
            return DASHBOARD_HTML

        @router.get("/reports/cost", dependencies=[reports])
        async def cost_report() -> dict[str, Any]:
            return get_runtime().analytics.dashboard()

        @router.post(
            "/v1/feedback", status_code=204,
            dependencies=[Depends(require("endpoint:feedback", "write"))],
        )
        async def feedback(payload: FeedbackRequest) -> None:
            try:
                get_runtime().analytics.record_feedback(payload.request_id, payload.quality_score)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        return router


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Semantic Cache Dashboard</title>
  <style>
    body { font: 16px system-ui, sans-serif; margin: 2rem auto; max-width: 900px; padding: 0 1rem; color: #172033; }
    h1 { margin-bottom: .25rem; } .muted { color: #5d687b; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1rem; margin: 1.5rem 0; }
    .card { border: 1px solid #dce2eb; border-radius: 10px; padding: 1rem; }
    .value { display: block; font-size: 1.7rem; font-weight: 650; margin-top: .4rem; }
    table { border-collapse: collapse; width: 100%; } th, td { padding: .7rem; text-align: left; border-bottom: 1px solid #dce2eb; }
    #error { color: #a11; }
  </style>
</head>
<body>
  <h1>Semantic Cache Dashboard</h1>
  <p class="muted">Live cache performance, estimated cost comparison, and A/B quality feedback.</p>
  <p id="error" role="status"></p>
  <section class="cards">
    <div class="card">Cache hit rate<span class="value" id="hit-rate">—</span></div>
    <div class="card">Requests / cache hits<span class="value" id="requests">—</span></div>
    <div class="card">Routed cost<span class="value" id="actual-cost">—</span></div>
    <div class="card">Estimated reduction<span class="value" id="reduction">—</span></div>
  </section>
  <h2>A/B experiment</h2>
  <table>
    <thead><tr><th>Arm</th><th>Requests</th><th>Average quality (1–5)</th><th>Routed cost</th></tr></thead>
    <tbody id="arms"></tbody>
  </table>
  <p class="muted" id="target"></p>
  <script>
    const money = value => "$" + Number(value).toFixed(4);
    async function refresh() {
      try {
        const response = await fetch("/dashboard");
        if (!response.ok) throw new Error("Dashboard data unavailable (" + response.status + ")");
        const data = await response.json();
        document.querySelector("#error").textContent = "";
        document.querySelector("#hit-rate").textContent = (data.cache_hit_rate * 100).toFixed(1) + "%";
        document.querySelector("#requests").textContent = data.requests + " / " + data.cache_hits;
        document.querySelector("#actual-cost").textContent = money(data.actual_cost_usd);
        document.querySelector("#reduction").textContent =
          data.cost_reduction_percent == null ? "Unknown" : data.cost_reduction_percent.toFixed(1) + "%";
        document.querySelector("#target").textContent =
          "Target reduction: " + data.target_cost_reduction_percent.minimum + "–" +
          data.target_cost_reduction_percent.maximum + "% (measured result is not guaranteed).";
        const body = document.querySelector("#arms");
        body.replaceChildren();
        for (const [name, arm] of Object.entries(data.ab_test)) {
          const row = document.createElement("tr");
          for (const value of [
            name, arm.requests,
            arm.average_quality_score == null ? "No feedback" : arm.average_quality_score.toFixed(2),
            money(arm.actual_cost_usd)
          ]) {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.append(cell);
          }
          body.append(row);
        }
      } catch (error) {
        document.querySelector("#error").textContent = error.message;
      }
    }
    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""


def _configure_system_trust() -> None:
    global _system_trust_configured
    if not _system_trust_configured:
        import truststore

        truststore.inject_into_ssl()
        _system_trust_configured = True


def _dense_vector_field(description: Any) -> str | None:
    schema = _field(description, "schema")
    fields = _field(schema, "fields", {}) or {}
    dense_fields = [
        name
        for name, field in fields.items()
        if _field(field, "dimension") is not None
    ]
    if not dense_fields:
        return None
    if len(dense_fields) > 1:
        raise ValueError(
            "Semantic cache requires a single Pinecone dense-vector schema field; "
            f"found {', '.join(dense_fields)}"
        )
    return dense_fields[0]


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _make_runtime(
    config: Settings,
    cache: Any,
    ollama: Any,
    analytics: AnalyticsStore,
    embedding_dimension: int | None = None,
) -> Runtime:
    rules = RoutingRuleEngine(config.routing_rules)
    router = ModelRouter(
        simple_model=config.simple_model,
        reasoning_model=config.reasoning_model,
        coding_model=config.coding_model,
        baseline_model=config.baseline_model,
        rules=rules,
        model_candidates=config.model_candidates,
        model_prices=config.model_prices,
    )
    return Runtime(config, cache, ollama, analytics, router, embedding_dimension)


def _should_bypass_cache(
    bypass_header: str | None, cache_control: str | None, pragma: str | None = None
) -> bool:
    if bypass_header and bypass_header.strip().lower() in {"1", "true", "yes"}:
        return True
    directives = {part.strip().lower() for part in (cache_control or "").split(",")}
    return bool(directives & {"no-cache", "no-store", "max-age=0"}) or (
        (pragma or "").strip().lower() == "no-cache"
    )


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _safe_request_id(value: str | None) -> str:
    """Accept caller-supplied ids only if they are short and log-safe; otherwise generate one."""
    return value if value and _REQUEST_ID_PATTERN.match(value) else str(uuid.uuid4())


def _scope_id(
    messages: list[dict[str, str]],
    category: str,
    model: str,
    options: dict[str, Any],
    access: str = "",
) -> str:
    scope = {
        "system": [message["content"] for message in messages if message["role"] == "system"],
        "category": category,
        "model": model,
        "options": options,
    }
    if access:
        scope["access"] = access
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _cost(prompt_tokens: int, completion_tokens: int, input_rate: float, output_rate: float) -> float:
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000
