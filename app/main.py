from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supertokens_python.exceptions import SuperTokensError
from supertokens_python.framework.fastapi import get_middleware
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import (
    session_exception_handler,
    verify_session,
)

from app.authentication.supertokens import (
    configure_supertokens,
    get_supertokens_cors_headers,
)
from app.config.database import Base, SessionLocal, engine
from app.middleware.audit import AuditMiddleware
from app.routes import admin_routes, auth_routes, inference_routes
from app.config.settings import Settings
from app.routes import semantic_proxy
from app.models.api_key import ApiKey  # noqa: F401  (registers table)
from app.models.audit_log import AuditLog  # noqa: F401  (registers table)


settings = configure_supertokens()

# Semantic cache + model routing, only reachable through the auth layer.
#  SEMANTIC_UPSTREAM_URL set   -> proxy to the stand-alone SemanticCaching&ModelRoutingLayer app
#  unset                       -> serve the embedded copy in semantic_router/
upstream_url = Settings().semantic_upstream_url
if upstream_url:
    semantic_router, semantic_lifespan = semantic_proxy.build_router(
        semantic_proxy.SemanticUpstream(upstream_url)
    )
else:
    from semantic_router.api import SemanticGateway

    _gateway = SemanticGateway()
    semantic_router, semantic_lifespan = _gateway.router, _gateway.lifespan

app = FastAPI(
    title="Enterprise Auth RBAC + Semantic Cache Gateway",
    lifespan=semantic_lifespan,
)

app.add_exception_handler(SuperTokensError, session_exception_handler)
app.add_middleware(get_middleware())
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.supertokens_website_domain or ""],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[*get_supertokens_cors_headers(), "x-cache-bypass"],
    # So the browser UI can read cache/routing details of a chat response.
    # SuperTokens' own headers must stay exposed or the browser SDK cannot see the session.
    expose_headers=[
        "front-token", "st-access-token", "st-refresh-token", "anti-csrf",
        "x-cache", "x-routed-model", "x-query-complexity", "x-experiment-arm", "x-request-id",
    ],
)

# Added last so it is the outermost middleware and sees the final status of every request.
app.add_middleware(AuditMiddleware, session_factory=SessionLocal)

app.include_router(inference_routes.router)
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(semantic_router)

Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/me")
async def current_user(
    session: SessionContainer = Depends(verify_session()),
):
    return {"user_id": session.get_user_id()}
 