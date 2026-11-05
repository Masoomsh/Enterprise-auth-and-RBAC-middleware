import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.authentication.base import Identity
from app.authentication.dependencies import app_settings, authorize_all, get_identity
from app.authorization.permissions import concrete_resource
from app.config.settings import Settings
from app.services.inference_service import InferenceBackend, get_inference_backend

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["inference"])

_ID = r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$"


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=100_000)


class InferenceRequest(BaseModel):
    model: str = Field(pattern=_ID)
    messages: list[Message] = Field(min_length=1, max_length=100)
    tools: list[str] = Field(default_factory=list, max_length=20)
    datasources: list[str] = Field(default_factory=list, max_length=20)


@router.post("/inference")
async def inference(
    body: InferenceRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(app_settings),
    backend: InferenceBackend = Depends(get_inference_backend),
):
    try:
        checks = [(concrete_resource("model", body.model), "invoke")]
        checks += [(concrete_resource("tool", t), "invoke") for t in sorted(set(body.tools))]
        checks += [
            (concrete_resource("datasource", d), "read") for d in sorted(set(body.datasources))
        ]
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tool or datasource identifier")

    # Never log prompt content; the audit layer records only this metadata.
    request.state.inference_meta = {
        "model": body.model,
        "tools": sorted(set(body.tools)),
        "datasources": sorted(set(body.datasources)),
        "message_count": len(body.messages),
        "prompt_chars": sum(len(m.content) for m in body.messages),
    }

    authorize_all(request, identity, checks, settings)

    try:
        result = await backend(
            body.model,
            [m.model_dump() for m in body.messages],
            sorted(set(body.tools)),
            sorted(set(body.datasources)),
        )
    except Exception:
        logger.exception("Inference backend failed")
        raise HTTPException(status_code=502, detail="Inference backend error")

    request.state.inference_meta["usage"] = result.get("usage")
    return {"model": body.model, "content": result["content"], "usage": result.get("usage")}
