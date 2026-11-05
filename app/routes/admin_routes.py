from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.authentication.dependencies import require
from app.config.database import get_db
from app.models.api_key import ApiKey
from app.services import identity_service
from app.services.identity_service import GATEWAY_ROLES

router = APIRouter(prefix="/admin", tags=["admin"])

_USER_ID = Path(pattern=r"^[A-Za-z0-9\-]{1,64}$")


class RoleBody(BaseModel):
    role: str


def _check_role(role: str) -> str:
    if role not in GATEWAY_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {list(GATEWAY_ROLES)}")
    return role


@router.get("/users/{user_id}/roles", dependencies=[Depends(require("admin:roles", "read"))])
async def get_roles(user_id: str = _USER_ID):
    return {"user_id": user_id, "roles": await identity_service.list_roles(user_id)}


@router.post("/users/{user_id}/roles", dependencies=[Depends(require("admin:roles", "write"))])
async def add_role(body: RoleBody, user_id: str = _USER_ID):
    await identity_service.assign_role(user_id, _check_role(body.role))
    return {"user_id": user_id, "roles": await identity_service.list_roles(user_id)}


@router.delete(
    "/users/{user_id}/roles/{role}", dependencies=[Depends(require("admin:roles", "write"))]
)
async def remove_role(role: str, user_id: str = _USER_ID):
    await identity_service.revoke_role(user_id, _check_role(role))
    return {"user_id": user_id, "roles": await identity_service.list_roles(user_id)}


@router.delete(
    "/api-keys/{prefix}",
    status_code=204,
    dependencies=[Depends(require("admin:api-keys", "write"))],
)
def revoke_any_key(prefix: str = Path(pattern=r"^[0-9a-f]{8}$"), db: Session = Depends(get_db)):
    row: Optional[ApiKey] = db.query(ApiKey).filter(ApiKey.prefix == prefix).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row.revoked = True
    db.commit()
