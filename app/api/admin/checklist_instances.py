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
from app.schemas.checklist_review import BulkReviewRequest, ItemReviewResponse, ItemReviewUpsert, ReviewContentCreate, ReviewContentResponse, ScoreUpdate
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


@router.get("/by-schedule/{schedule_id}", response_model=ChecklistInstanceDetailResponse)
async def get_instance_by_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """스케줄 ID로 체크리스트 인스턴스 상세를 조회합니다."""
    from app.repositories.checklist_instance_repository import checklist_instance_repository
    instance = await checklist_instance_repository.get_by_schedule_id(db, schedule_id)
    if not instance:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No checklist instance for this schedule")
    return await checklist_instance_service.build_detail_response(db, instance)


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


@router.get("/review-summary")
async def get_review_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    store_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> dict:
    """체크리스트 리뷰 요약 통계를 조회합니다.

    Get aggregated review summary counts for a date range.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user with checklists:read)
        store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
        date_from: 시작일 필터, 선택 (Optional start date filter)
        date_to: 종료일 필터, 선택 (Optional end date filter)

    Returns:
        dict: 리뷰 요약 통계 (Review summary counts)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None

    return await checklist_instance_service.get_review_summary(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        date_from=date_from,
        date_to=date_to,
    )


@router.patch("/{instance_id}/score")
async def update_score(
    instance_id: UUID,
    data: ScoreUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklist_review:create"))],
) -> dict:
    """인스턴스에 점수를 부여하거나 수정합니다."""
    instance = await checklist_instance_service.update_score(
        db,
        instance_id=instance_id,
        organization_id=current_user.organization_id,
        scorer_id=current_user.id,
        score=data.score,
        score_note=data.score_note,
    )
    return {
        "id": str(instance.id),
        "score": instance.score,
        "score_note": instance.score_note,
        "scored_by": str(instance.scored_by) if instance.scored_by else None,
        "scored_at": instance.scored_at,
    }


@router.post("/{instance_id}/items/bulk-review")
async def bulk_review(
    instance_id: UUID,
    data: BulkReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklist_review:create"))],
) -> dict:
    """여러 항목에 리뷰 결과를 일괄 적용합니다."""
    reviewed = await checklist_instance_service.bulk_review(
        db,
        instance_id=instance_id,
        organization_id=current_user.organization_id,
        reviewer_id=current_user.id,
        item_indexes=data.item_indexes,
        result=data.result,
    )
    return {
        "reviewed_count": len(reviewed),
        "item_indexes": [item.item_index for item in reviewed],
    }


@router.post("/{instance_id}/report")
async def send_report(
    instance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """체크리스트 인스턴스 리포트를 전송합니다 (stub)."""
    return {"message": "Report sent"}


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
    current_user: Annotated[User, Depends(require_permission("checklist_review:create"))],
) -> dict:
    """아이템 리뷰를 생성하거나 수정합니다 (upsert). 인라인 코멘트 옵션 포함."""
    review = await checklist_instance_service.upsert_review(
        db,
        instance_id=instance_id,
        item_index=item_index,
        reviewer_id=current_user.id,
        result=data.result,
        comment_text=data.comment_text,
        comment_photo_url=data.comment_photo_url,
    )

    return {
        "id": str(review.id),
        "instance_id": str(instance_id),
        "item_index": item_index,
        "reviewer_id": str(review.reviewer_id),
        "reviewer_name": current_user.full_name,
        "result": review.review_result,
        "contents": [],
        "history": [],
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


@router.delete("/{instance_id}/items/{item_index}/review", response_model=MessageResponse)
async def delete_review(
    instance_id: UUID,
    item_index: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklist_review:delete"))],
) -> dict:
    """아이템 리뷰를 삭제합니다."""
    await checklist_instance_service.delete_review(db, instance_id, item_index, current_user.id)
    return {"message": "Review deleted"}


@router.post("/{instance_id}/items/{item_index}/review/contents", response_model=ReviewContentResponse)
async def add_review_content(
    instance_id: UUID,
    item_index: int,
    data: ReviewContentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklist_review:create"))],
) -> dict:
    """리뷰에 콘텐츠(텍스트/사진/영상)를 추가합니다."""
    rc = await checklist_instance_service.add_review_content(
        db,
        instance_id=instance_id,
        item_index=item_index,
        author_id=current_user.id,
        content_type=data.type,
        content=data.content,
    )

    review_id = getattr(rc, "review_id", rc.item_id)
    return {
        "id": str(rc.id),
        "review_id": str(review_id),
        "author_id": str(rc.author_id),
        "author_name": current_user.full_name,
        "type": rc.type,
        "content": rc.content,
        "created_at": rc.created_at,
    }


@router.delete("/{instance_id}/items/{item_index}/review/contents/{content_id}", response_model=MessageResponse)
async def delete_review_content(
    instance_id: UUID,
    item_index: int,
    content_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklist_review:delete"))],
) -> dict:
    """리뷰 콘텐츠를 삭제합니다."""
    await checklist_instance_service.delete_review_content(db, content_id)
    return {"message": "Content deleted"}
