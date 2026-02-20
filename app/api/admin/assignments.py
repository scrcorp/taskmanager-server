"""관리자 업무 배정 라우터 — 업무 배정 관리 API.

Admin Assignment Router — API endpoints for work assignment management.
Provides CRUD operations including bulk creation and filtering.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    AssignmentCreate,
    AssignmentDetailResponse,
    AssignmentResponse,
    MessageResponse,
    PaginatedResponse,
)
from app.services.assignment_service import assignment_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_assignments(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    work_date: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """업무 배정 목록을 필터링하여 조회합니다.

    List work assignments with optional filters.

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
        dict: 페이지네이션된 배정 목록 (Paginated assignment list)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    assignments, total = await assignment_service.list_assignments(
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
    for a in assignments:
        response: dict = await assignment_service.build_response(db, a)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/recent-users")
async def list_recent_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    store_id: Annotated[str, Query()],
    exclude_date: Annotated[date | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict:
    """매장 내 최근 배정된 사용자 목록을 조회합니다.

    Get recently assigned user IDs per shift x position combo.
    Used by the admin schedule page to prioritize recent workers.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        store_id: 매장 UUID (Store UUID, required)
        exclude_date: 제외할 날짜 (Date to exclude, e.g. today)
        days: 조회 기간, 기본 30일, 최대 90일 (Lookback period, default 30, max 90)

    Returns:
        dict: { items: [{ shift_id, position_id, user_id, last_work_date }] }
    """
    store_uuid: UUID = UUID(store_id)

    items: list[dict] = await assignment_service.get_recent_users(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        exclude_date=exclude_date,
        days=days,
    )

    return {"items": items}


@router.get("/{assignment_id}", response_model=AssignmentDetailResponse)
async def get_assignment(
    assignment_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """업무 배정 상세를 조회합니다.

    Get work assignment detail with checklist snapshot.

    Args:
        assignment_id: 배정 UUID 문자열 (Assignment UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 배정 상세 (Assignment detail)
    """
    assignment = await assignment_service.get_detail(
        db,
        assignment_id=assignment_id,
        organization_id=current_user.organization_id,
    )

    return await assignment_service.build_detail_response(db, assignment)


@router.post("", response_model=AssignmentDetailResponse, status_code=201)
async def create_assignment(
    data: AssignmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """새 업무 배정을 생성합니다.

    Create a new work assignment with checklist snapshot.

    Args:
        data: 배정 생성 데이터 (Assignment creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 배정 상세 (Created assignment detail)
    """
    assignment = await assignment_service.create_assignment(
        db,
        organization_id=current_user.organization_id,
        data=data,
        assigned_by=current_user.id,
    )
    await db.commit()

    return await assignment_service.build_detail_response(db, assignment)


@router.post("/bulk", response_model=list[AssignmentResponse], status_code=201)
async def bulk_create_assignments(
    data: list[AssignmentCreate],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[dict]:
    """여러 업무 배정을 일괄 생성합니다.

    Bulk create multiple work assignments.

    Args:
        data: 배정 생성 데이터 목록 (List of assignment creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        list[dict]: 생성된 배정 목록 (List of created assignments)
    """
    assignments = await assignment_service.bulk_create(
        db,
        organization_id=current_user.organization_id,
        assignments_data=data,
        assigned_by=current_user.id,
    )
    await db.commit()

    items: list[dict] = []
    for a in assignments:
        response: dict = await assignment_service.build_response(db, a)
        items.append(response)

    return items


@router.delete("/{assignment_id}", response_model=MessageResponse)
async def delete_assignment(
    assignment_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """업무 배정을 삭제합니다.

    Delete a work assignment.

    Args:
        assignment_id: 배정 UUID 문자열 (Assignment UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    await assignment_service.delete_assignment(
        db,
        assignment_id=assignment_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "업무 배정이 삭제되었습니다 (Work assignment deleted)"}
