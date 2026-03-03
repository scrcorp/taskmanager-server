"""앱 업무 배정 라우터 — 내 업무 배정 API.

App Assignment Router — API endpoints for user's own work assignments.
Provides read-only access and checklist item completion for the mobile app.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    AssignmentDetailResponse,
    ChecklistItemComplete,
    ChecklistItemRespond,
)
from app.services.assignment_service import assignment_service
from app.services.checklist_instance_service import checklist_instance_service
from app.repositories.checklist_instance_repository import checklist_instance_repository
from app.utils.exceptions import NotFoundError, ForbiddenError

router: APIRouter = APIRouter()


@router.get("/work-assignments")
async def list_my_assignments(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    per_page: Annotated[int | None, Query(ge=1, le=50)] = None,
) -> list[dict] | dict:
    """내 업무 배정 목록을 조회합니다.

    List my work assignments with optional filters.

    - Single date mode (work_date): returns List[AssignmentResponse] (backward compatible)
    - Date range mode (date_from/date_to): returns paginated response with items/total/page/per_page
    """
    is_range_mode: bool = date_from is not None or date_to is not None

    if is_range_mode:
        actual_page: int = page or 1
        actual_per_page: int = per_page or 20
        result = await assignment_service.get_my_assignments(
            db,
            user_id=current_user.id,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=actual_page,
            per_page=actual_per_page,
        )
        assignments, total = result  # type: ignore[misc]

        items: list[dict] = []
        for a in assignments:
            items.append(await assignment_service.build_response(db, a))

        return {"items": items, "total": total, "page": actual_page, "per_page": actual_per_page}

    # 단일 날짜 모드 — Single date mode (backward compatible)
    assignments_list = await assignment_service.get_my_assignments(
        db,
        user_id=current_user.id,
        work_date=work_date,
        status=status,
    )

    items = []
    for a in assignments_list:  # type: ignore[union-attr]
        items.append(await assignment_service.build_response(db, a))

    return items


@router.get("/work-assignments/{assignment_id}", response_model=AssignmentDetailResponse)
async def get_my_assignment(
    assignment_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 업무 배정 상세를 조회합니다.

    Get my work assignment detail with checklist snapshot.

    Args:
        assignment_id: 배정 UUID (Assignment UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 배정 상세 (Assignment detail with checklist snapshot)
    """
    assignment = await assignment_service.get_detail(
        db,
        assignment_id=assignment_id,
        organization_id=current_user.organization_id,
    )

    # 본인 배정만 조회 가능 — Only allow viewing own assignments
    if assignment.user_id != current_user.id:
        raise ForbiddenError("Can only view your own assignment")

    return await assignment_service.build_detail_response(db, assignment)


@router.patch(
    "/work-assignments/{assignment_id}/checklist/{item_index}",
    response_model=AssignmentDetailResponse,
)
async def complete_checklist_item(
    assignment_id: UUID,
    item_index: int,
    data: ChecklistItemComplete,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """체크리스트 항목을 완료/미완료 처리합니다.

    Complete or uncomplete a checklist item.

    Args:
        assignment_id: 배정 UUID 문자열 (Assignment UUID string)
        item_index: 체크리스트 항목 인덱스 (Checklist item index)
        data: 완료 여부 데이터 (Completion status data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 업데이트된 배정 상세 (Updated assignment detail)
    """
    assignment = await assignment_service.complete_checklist_item(
        db,
        assignment_id=assignment_id,
        user_id=current_user.id,
        item_index=item_index,
        is_completed=data.is_completed,
        client_timezone=data.timezone,
        photo_url=data.photo_url,
        note=data.note,
    )
    await db.commit()

    return await assignment_service.build_detail_response(db, assignment)


@router.patch(
    "/work-assignments/{assignment_id}/checklist/{item_index}/respond",
    response_model=AssignmentDetailResponse,
)
async def respond_to_rejection(
    assignment_id: UUID,
    item_index: int,
    data: ChecklistItemRespond,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """거절된 체크리스트 항목에 대해 재제출합니다.

    Respond to a rejected checklist item by resubmitting with new evidence.
    Wraps the checklist-instance resubmit flow using assignment_id.
    """
    instance = await checklist_instance_repository.get_by_assignment_id(
        db, assignment_id
    )
    if instance is None:
        raise NotFoundError("Checklist instance not found for this assignment")
    if instance.user_id != current_user.id:
        raise ForbiddenError("Can only respond to your own assignment")

    await checklist_instance_service.resubmit_completion(
        db,
        instance_id=instance.id,
        item_index=item_index,
        user_id=current_user.id,
        photo_url=data.photo_url,
        note=data.response_comment,
        client_timezone=data.timezone,
    )
    await db.commit()

    assignment = await assignment_service.get_detail(
        db,
        assignment_id=assignment_id,
        organization_id=current_user.organization_id,
    )
    return await assignment_service.build_detail_response(db, assignment)
