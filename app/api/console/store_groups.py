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
    NumberingRecalculateRequest,
    NumberingRecalculateResponse,
    NumberingUpdateRequest,
    NumberingUpdateResponse,
    GroupAssignPreviewRequest,
    GroupAssignPreviewResponse,
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


@router.post("/assign-preview", response_model=GroupAssignPreviewResponse)
async def preview_group_assign(
    data: GroupAssignPreviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> GroupAssignPreviewResponse:
    """편입 미리보기 — 매장을 그룹에 넣으면 생길 EMPID 충돌을 조회만 합니다 (읽기 전용).

    편입 자체는 번호를 절대 바꾸지 않으므로(정책 A) 저장 전 경고 용도.
    group_id null(이탈) 또는 mode="store" 그룹은 충돌 개념이 없어 빈 배열.
    """
    return await store_group_service.assign_preview(
        db, current_user.organization_id, data.store_id, data.group_id
    )


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


@router.put("/{group_id}/numbering", response_model=NumberingUpdateResponse)
async def update_group_numbering(
    group_id: UUID,
    data: NumberingUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> NumberingUpdateResponse:
    """그룹 채번 커서(다음 발급 번호)를 수동 조정합니다 (§3-2).

    사유 필수(ERR_REASON_REQUIRED). 낮추는 것도 허용하되 lowered=true 로 알린다
    — INV-2(커서는 전진만)의 유일한 예외가 운영자의 명시 조작이고, 그래서 사유와
    이력(empid_changes, source='cursor')을 강제한다.
    """
    return await store_group_service.update_numbering(
        db, group_id, current_user.organization_id, data, current_user.id
    )


@router.post(
    "/{group_id}/numbering/recalculate", response_model=NumberingRecalculateResponse
)
async def recalculate_group_numbering(
    group_id: UUID,
    data: NumberingRecalculateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> NumberingRecalculateResponse:
    """그룹 채번 커서를 재계산합니다 (§3-3). apply=false 면 미리보기만.

    "예외 N건은 계산에서 제외됨" 문구의 N 이 응답의 exception_count 다.
    """
    return await store_group_service.recalculate_numbering(
        db, group_id, current_user.organization_id, data, current_user.id
    )


@router.delete("/{group_id}", status_code=204)
async def delete_store_group(
    group_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:delete"))],
) -> None:
    """그룹을 삭제합니다. 소속 매장은 미그룹으로 남는다 (empid 불변)."""
    await store_group_service.delete_group(db, group_id, current_user.organization_id)
