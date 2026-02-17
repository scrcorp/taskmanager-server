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
    AssignmentResponse,
    ChecklistItemComplete,
)
from app.services.assignment_service import assignment_service

router: APIRouter = APIRouter()


@router.get("/work-assignments", response_model=list[AssignmentResponse])
async def list_my_assignments(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """내 업무 배정 목록을 조회합니다.

    List my work assignments with optional filters.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        work_date: 근무일 필터, 선택 (Optional work date filter)
        status: 상태 필터, 선택 (Optional status filter)

    Returns:
        list[dict]: 내 배정 목록 (My assignment list)
    """
    assignments = await assignment_service.get_my_assignments(
        db,
        user_id=current_user.id,
        work_date=work_date,
        status=status,
    )

    items: list[dict] = []
    for a in assignments:
        response: dict = await assignment_service.build_response(db, a)
        items.append(response)

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
        from app.utils.exceptions import ForbiddenError

        raise ForbiddenError("본인의 배정만 조회할 수 있습니다 (Can only view your own assignment)")

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
    )
    await db.commit()

    return await assignment_service.build_detail_response(db, assignment)
