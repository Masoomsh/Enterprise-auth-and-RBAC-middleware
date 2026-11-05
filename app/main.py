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
from app.models.api_key import ApiKey  # noqa: F401  (registers table)
from app.models.audit_log import AuditLog  # noqa: F401  (registers table)


settings = configure_supertokens()

app = FastAPI(
    title="Enterprise Auth RBAC"
)

app.add_exception_handler(SuperTokensError, session_exception_handler)
app.add_middleware(get_middleware())
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.supertokens_website_domain or ""],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=get_supertokens_cors_headers(),
)

# Added last so it is the outermost middleware and sees the final status of every request.
app.add_middleware(AuditMiddleware, session_factory=SessionLocal)

app.include_router(inference_routes.router)
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)

Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/me")
async def current_user(
    session: SessionContainer = Depends(verify_session()),
):
    return {"user_id": session.get_user_id()}
 