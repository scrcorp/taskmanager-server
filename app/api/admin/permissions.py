"""관리자 권한 라우터 — Permission 조회 및 역할별 권한 관리.

Admin Permission Router — Permission listing and role-permission management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.services.permission_service import permission_service

router: APIRouter = APIRouter()


class UpdateRolePermissionsRequest(BaseModel):
    permission_codes: list[str]


@router.get("")
async def list_permissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("roles:read"))],
) -> list[dict]:
    """전체 permission 목록 조회."""
    return await permission_service.list_all_permissions(db)


@router.get("/roles/{role_id}")
async def get_role_permissions(
    role_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("roles:read"))],
) -> list[dict]:
    """역할의 permission 목록 조회."""
    org_id: UUID = current_user.organization_id
    return await permission_service.get_role_permissions(db, role_id, org_id)


@router.put("/roles/{role_id}")
async def update_role_permissions(
    role_id: UUID,
    data: UpdateRolePermissionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("roles:update"))],
) -> list[dict]:
    """역할의 permission 일괄 업데이트."""
    org_id: UUID = current_user.organization_id
    result = await permission_service.update_role_permissions(
        db, role_id, data.permission_codes, org_id, caller=current_user
    )
    await db.commit()
    return result
