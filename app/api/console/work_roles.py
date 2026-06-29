"""관리자 업무 역할 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.schedule import StoreWorkRole
from app.models.user import User
from app.schemas.schedule import WorkRoleCreate, WorkRoleReorderRequest, WorkRoleResponse, WorkRoleUpdate
from app.services.work_role_service import work_role_service
from app.utils.exceptions import NotFoundError

router: APIRouter = APIRouter()


async def _check_work_role_store_access(
    db: AsyncSession, current_user: User, work_role_id: UUID
) -> None:
    """flat work-role 경로용 — work_role 의 store 에 접근 가능한지 검증.

    org 내 미할당 매장의 work-role 을 GM/SV 가 수정/삭제하지 못하게 차단.
    org 소속 검증은 service 가 처리(미소속이면 NotFound).
    """
    store_id = await db.scalar(
        select(StoreWorkRole.store_id).where(StoreWorkRole.id == work_role_id)
    )
    if store_id is None:
        raise NotFoundError("Work role not found")
    await check_store_access(db, current_user, store_id)


@router.get("/stores/{store_id}/work-roles", response_model=list[WorkRoleResponse])
async def list_work_roles(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[WorkRoleResponse]:
    """매장의 업무 역할 목록을 조회합니다."""
    await check_store_access(db, current_user, store_id)
    return await work_role_service.list_work_roles(
        db, store_id, current_user.organization_id
    )


@router.post(
    "/stores/{store_id}/work-roles",
    response_model=WorkRoleResponse,
    status_code=201,
)
async def create_work_role(
    store_id: UUID,
    data: WorkRoleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:create"))],
) -> WorkRoleResponse:
    """새 업무 역할을 생성합니다."""
    await check_store_access(db, current_user, store_id)
    return await work_role_service.create_work_role(
        db, store_id, current_user.organization_id, data
    )


@router.put("/work-roles/{work_role_id}", response_model=WorkRoleResponse)
async def update_work_role(
    work_role_id: UUID,
    data: WorkRoleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> WorkRoleResponse:
    """업무 역할을 수정합니다."""
    await _check_work_role_store_access(db, current_user, work_role_id)
    return await work_role_service.update_work_role(
        db, work_role_id, current_user.organization_id, data
    )


@router.put(
    "/stores/{store_id}/work-roles/reorder",
    response_model=list[WorkRoleResponse],
)
async def reorder_work_roles(
    store_id: UUID,
    data: WorkRoleReorderRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> list[WorkRoleResponse]:
    """업무 역할 순서를 일괄 변경합니다."""
    await check_store_access(db, current_user, store_id)
    return await work_role_service.reorder_work_roles(
        db, store_id, current_user.organization_id,
        [{"id": item.id, "sort_order": item.sort_order} for item in data.items],
    )


@router.delete("/work-roles/{work_role_id}", status_code=204)
async def delete_work_role(
    work_role_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:delete"))],
) -> None:
    """업무 역할을 삭제합니다."""
    await _check_work_role_store_access(db, current_user, work_role_id)
    await work_role_service.delete_work_role(
        db, work_role_id, current_user.organization_id
    )
