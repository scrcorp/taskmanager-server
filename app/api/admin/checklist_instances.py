"""관리자 체크리스트 인스턴스 라우터 — 체크리스트 인스턴스 관리 API.

Admin Checklist Instance Router — API endpoints for checklist instance management.
Provides list and detail views for supervisors and above.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    ChecklistInstanceDetailResponse,
    ChecklistInstanceResponse,
    MessageResponse,
    PaginatedResponse,
)
from app.schemas.checklist_review import ItemReviewResponse, ItemReviewUpsert
from app.services.checklist_instance_service import checklist_instance_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_checklist_instances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    work_date: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """체크리스트 인스턴스 목록을 필터링하여 조회합니다.

    List checklist instances with optional filters.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
        user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
        work_date: 근무일 필터, 선택 (Optional work date filter)
        status: 상태 필터, 선택 (Optional status filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 인스턴스 목록 (Paginated instance list)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    instances, total = await checklist_instance_service.get_instances(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        user_id=user_uuid,
        work_date=work_date,
        status=status,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = []
    for inst in instances:
        response: dict = await checklist_instance_service.build_response(db, inst)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/checklist-audit")
@router.get("/completion-log")
async def get_completion_log(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """Get checklist completion log showing who completed what items, when.

    Requires checklists:read permission (GM+).
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    items, total = await checklist_instance_service.get_completion_log(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        user_id=user_uuid,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{instance_id}", response_model=ChecklistInstanceDetailResponse)
async def get_checklist_instance(
    instance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """체크리스트 인스턴스 상세를 조회합니다 (스냅샷 + 완료 기록 병합).

    Get checklist instance detail with snapshot merged with completions.

    Args:
        instance_id: 인스턴스 UUID (Instance UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 인스턴스 상세 (Instance detail with merged snapshot)
    """
    instance = await checklist_instance_service.get_instance(
        db,
        instance_id=instance_id,
        organization_id=current_user.organization_id,
    )

    return await checklist_instance_service.build_detail_response(db, instance)


@router.get("/{instance_id}/reviews", response_model=list[ItemReviewResponse])
async def list_reviews(
    instance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> list[dict]:
    """인스턴스의 전체 아이템 리뷰 목록을 조회합니다."""
    return await checklist_instance_service.get_reviews_for_instance(db, instance_id)


@router.put("/{instance_id}/items/{item_index}/review", response_model=ItemReviewResponse)
async def upsert_review(
    instance_id: UUID,
    item_index: int,
    data: ItemReviewUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """아이템 리뷰를 생성하거나 수정합니다 (upsert)."""
    review = await checklist_instance_service.upsert_review(
        db,
        instance_id=instance_id,
        item_index=item_index,
        reviewer_id=current_user.id,
        result=data.result,
        comment=data.comment,
        photo_url=data.photo_url,
    )
    await db.commit()

    return {
        "id": str(review.id),
        "instance_id": str(review.instance_id),
        "item_index": review.item_index,
        "reviewer_id": str(review.reviewer_id),
        "reviewer_name": current_user.full_name,
        "result": review.result,
        "comment": review.comment,
        "photo_url": review.photo_url,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


@router.delete("/{instance_id}/items/{item_index}/review", response_model=MessageResponse)
async def delete_review(
    instance_id: UUID,
    item_index: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """아이템 리뷰를 삭제합니다."""
    await checklist_instance_service.delete_review(db, instance_id, item_index)
    await db.commit()
    return {"message": "리뷰가 삭제되었습니다 (Review deleted)"}
