"""관리자 체크리스트 인스턴스 라우터 — 체크리스트 인스턴스 관리 API.

Admin Checklist Instance Router — API endpoints for checklist instance management.
Provides list and detail views for supervisors and above.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.api.deps import require_gm, require_supervisor
from app.database import get_db
from app.models.checklist import ChecklistComment
from app.models.user import User
from app.schemas.common import (
    ChecklistInstanceDetailResponse,
    ChecklistInstanceResponse,
    PaginatedResponse,
)
from app.schemas.checklist_comment import ChecklistCommentCreate, ChecklistCommentResponse
from app.services.checklist_instance_service import checklist_instance_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_checklist_instances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
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
async def get_checklist_audit(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """체크리스트 완료 감사 로그를 조회합니다.

    Get checklist completion audit log showing who completed what items, when.
    Requires GM or higher permission.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)
        store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
        user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
        date_from: 시작일 필터, 선택 (Optional start date filter)
        date_to: 종료일 필터, 선택 (Optional end date filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 감사 로그 (Paginated audit log)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    items, total = await checklist_instance_service.get_audit_log(
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
    current_user: Annotated[User, Depends(require_supervisor)],
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


@router.get("/{instance_id}/comments", response_model=list[ChecklistCommentResponse])
async def list_comments(
    instance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[ChecklistCommentResponse]:
    """체크리스트 인스턴스의 코멘트 목록을 조회합니다."""
    query = (
        select(ChecklistComment)
        .where(ChecklistComment.instance_id == instance_id)
        .order_by(ChecklistComment.created_at)
    )
    result = await db.execute(query)
    comments = list(result.scalars().all())

    responses: list[ChecklistCommentResponse] = []
    for c in comments:
        user_result = await db.execute(select(User).where(User.id == c.user_id))
        user = user_result.scalar_one_or_none()
        responses.append(ChecklistCommentResponse(
            id=str(c.id),
            instance_id=str(c.instance_id),
            user_id=str(c.user_id),
            user_name=user.full_name if user else None,
            text=c.text,
            created_at=c.created_at,
        ))
    return responses


@router.post("/{instance_id}/comments", response_model=ChecklistCommentResponse, status_code=201)
async def create_comment(
    instance_id: UUID,
    data: ChecklistCommentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> ChecklistCommentResponse:
    """체크리스트 인스턴스에 코멘트를 추가합니다."""
    comment = ChecklistComment(
        instance_id=instance_id,
        user_id=current_user.id,
        text=data.text,
    )
    db.add(comment)
    await db.flush()
    await db.refresh(comment)
    await db.commit()

    return ChecklistCommentResponse(
        id=str(comment.id),
        instance_id=str(comment.instance_id),
        user_id=str(comment.user_id),
        user_name=current_user.full_name,
        text=comment.text,
        created_at=comment.created_at,
    )
