"""관리자 역할 라우터 — 역할 CRUD 엔드포인트.

Admin Role Router — CRUD endpoints for role management.
Only accessible by admin-level users.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_gm
from app.database import get_db
from app.models.user import User
from app.schemas.user import RoleCreate, RoleResponse, RoleUpdate
from app.services.role_service import role_service

router: APIRouter = APIRouter()


@router.get("", response_model=list[RoleResponse])
async def list_roles(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> list[RoleResponse]:
    """역할 목록을 조회합니다.

    List all roles in the current organization.
    """
    org_id: UUID = current_user.organization_id
    return await role_service.list_roles(db, org_id)


@router.post("", response_model=RoleResponse, status_code=201)
async def create_role(
    data: RoleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> RoleResponse:
    """새 역할을 생성합니다.

    Create a new role in the current organization.
    """
    org_id: UUID = current_user.organization_id
    result: RoleResponse = await role_service.create_role(
        db, org_id, data, caller_level=current_user.role.level
    )
    await db.commit()
    return result


@router.put("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: UUID,
    data: RoleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> RoleResponse:
    """역할 정보를 수정합니다.

    Update an existing role.
    """
    org_id: UUID = current_user.organization_id
    result: RoleResponse = await role_service.update_role(
        db, role_id, org_id, data, caller_level=current_user.role.level
    )
    await db.commit()
    return result


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> None:
    """역할을 삭제합니다.

    Delete a role by its ID.
    """
    org_id: UUID = current_user.organization_id
    await role_service.delete_role(db, role_id, org_id, caller_level=current_user.role.level)
    await db.commit()
