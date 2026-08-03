"""관리자 매장 그룹 라우터 — 그룹 CRUD/정렬 엔드포인트.

Admin Store Group Router — CRUD + reorder endpoints for store groups.
All endpoints are scoped to the current organization from JWT.

Permission Matrix (매장과 동일 권한 재사용):
    - 그룹 목록: stores:read / 생성: stores:create / 수정·정렬: stores:update / 삭제: stores:delete
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.organization import (
    StoreGroupCreate,
    StoreGroupReorderRequest,
    StoreGroupResponse,
    StoreGroupUpdate,
)
from app.services.store_group_service import store_group_service

router: APIRouter = APIRouter()


@router.put("/reorder", status_code=204)
async def reorder_store_groups(
    data: StoreGroupReorderRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> None:
    """그룹 표시 순서를 일괄 변경합니다 (드래그 정렬). /{group_id} 보다 먼저 선언."""
    org_id: UUID = current_user.organization_id
    await store_group_service.reorder_groups(db, org_id, data.group_ids)


@router.get("", response_model=list[StoreGroupResponse])
async def list_store_groups(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[StoreGroupResponse]:
    """그룹 목록을 조회합니다 — sort_order 순, 소속 매장 수 포함."""
    return await store_group_service.list_groups(db, current_user.organization_id)


@router.post("", response_model=StoreGroupResponse, status_code=201)
async def create_store_group(
    data: StoreGroupCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:create"))],
) -> StoreGroupResponse:
    """새 그룹을 생성합니다."""
    return await store_group_service.create_group(db, current_user.organization_id, data)


@router.put("/{group_id}", response_model=StoreGroupResponse)
async def update_store_group(
    group_id: UUID,
    data: StoreGroupUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> StoreGroupResponse:
    """그룹을 수정합니다. 공유 모드면 스코프 내 기존 empid 중복을 duplicate_empids 로 경고."""
    return await store_group_service.update_group(
        db, group_id, current_user.organization_id, data
    )


@router.delete("/{group_id}", status_code=204)
async def delete_store_group(
    group_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:delete"))],
) -> None:
    """그룹을 삭제합니다. 소속 매장은 미그룹으로 남는다 (empid 불변)."""
    await store_group_service.delete_group(db, group_id, current_user.organization_id)
