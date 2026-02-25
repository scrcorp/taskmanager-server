"""관리자 스케줄 라우터 — 스케줄 관리 API.

Admin Schedule Router — API endpoints for schedule management.
Provides CRUD operations, status transitions (submit, approve, cancel),
and filtered listing for schedule drafts and approvals.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    MessageResponse,
    OvertimeValidateRequest,
    PaginatedResponse,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleSubstituteRequest,
    ScheduleUpdate,
)
from app.services.schedule_service import schedule_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_schedules(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    work_date: Annotated[date | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """스케줄 목록을 필터링하여 조회합니다.

    List schedules with optional filters.
    date_from/date_to 범위 필터가 있으면 work_date보다 우선합니다.
    (Date range filters take precedence over single work_date.)
    Accessible by Supervisor+ (level <= 3).

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
        user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
        work_date: 근무일 필터, 선택 (Optional work date filter)
        date_from: 시작일 범위 필터, 선택 (Optional range start date)
        date_to: 종료일 범위 필터, 선택 (Optional range end date)
        status: 상태 필터, 선택 (Optional status filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 스케줄 목록 (Paginated schedule list)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    schedules, total = await schedule_service.get_schedules(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        user_id=user_uuid,
        work_date=work_date,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = []
    for s in schedules:
        response: dict = await schedule_service.build_response(db, s)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """스케줄 상세를 조회합니다.

    Get schedule detail with resolved entity names.
    Accessible by Supervisor+ (level <= 3).

    Args:
        schedule_id: 스케줄 UUID (Schedule UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 스케줄 상세 (Schedule detail)
    """
    schedule = await schedule_service.get_schedule(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
    )

    return await schedule_service.build_response(db, schedule)


@router.post("", response_model=ScheduleResponse, status_code=201)
async def create_schedule(
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """새 스케줄 초안을 생성합니다.

    Create a new draft schedule.
    Accessible by Supervisor+ (level <= 3). SV creates draft.

    Args:
        data: 스케줄 생성 데이터 (Schedule creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 스케줄 상세 (Created schedule detail)
    """
    schedule = await schedule_service.create_schedule(
        db,
        organization_id=current_user.organization_id,
        data=data,
        created_by=current_user.id,
    )
    await db.commit()

    return await schedule_service.build_response(db, schedule)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: UUID,
    data: ScheduleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """스케줄을 수정합니다.

    Update a schedule (draft or pending status only).
    Accessible by Supervisor+ (level <= 3).
    SV can edit draft, GM+ can edit pending.

    Args:
        schedule_id: 스케줄 UUID (Schedule UUID)
        data: 스케줄 수정 데이터 (Schedule update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 수정된 스케줄 상세 (Updated schedule detail)
    """
    schedule = await schedule_service.update_schedule(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
        data=data,
        user_id=current_user.id,
    )
    await db.commit()

    return await schedule_service.build_response(db, schedule)


@router.post("/{schedule_id}/submit", response_model=ScheduleResponse)
async def submit_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """스케줄을 승인 요청합니다 (draft → pending).

    Submit a schedule for approval (status: draft -> pending).
    Accessible by Supervisor+ (level <= 3).

    Args:
        schedule_id: 스케줄 UUID (Schedule UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 상태 변경된 스케줄 상세 (Schedule with updated status)
    """
    schedule = await schedule_service.submit_for_approval(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return await schedule_service.build_response(db, schedule)


@router.post("/{schedule_id}/approve", response_model=ScheduleResponse)
async def approve_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> dict:
    """스케줄을 승인하고 work_assignment를 자동 생성합니다.

    Approve a schedule and auto-create a work_assignment.
    Accessible by Owner/GM only (level <= 2).

    Args:
        schedule_id: 스케줄 UUID (Schedule UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)

    Returns:
        dict: 승인된 스케줄 상세 (Approved schedule detail with work_assignment_id)
    """
    schedule = await schedule_service.approve_schedule(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
        approved_by=current_user.id,
    )
    await db.commit()

    return await schedule_service.build_response(db, schedule)


@router.post("/{schedule_id}/cancel", response_model=MessageResponse)
async def cancel_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """스케줄을 취소합니다.

    Cancel a schedule (draft or pending status only).
    Accessible by Supervisor+ (level <= 3).
    SV can cancel draft, GM+ can cancel pending.

    Args:
        schedule_id: 스케줄 UUID (Schedule UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 취소 결과 메시지 (Cancellation result message)
    """
    await schedule_service.cancel_schedule(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
        user_id=current_user.id,
    )
    await db.commit()

    return {"message": "스케줄이 취소되었습니다 (Schedule cancelled)"}


@router.patch("/{schedule_id}/substitute", response_model=ScheduleResponse)
async def substitute_schedule(
    schedule_id: UUID,
    data: ScheduleSubstituteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """대타 처리 — 승인된 스케줄의 담당자를 변경합니다. Owner + GM만 가능.

    Substitute schedule — Change the assigned user of an approved schedule.
    Owner + GM only.
    """
    schedule = await schedule_service.substitute_schedule(
        db,
        schedule_id=schedule_id,
        organization_id=current_user.organization_id,
        new_user_id=UUID(data.new_user_id),
        requested_by=current_user.id,
    )
    await db.commit()

    return await schedule_service.build_response(db, schedule)


@router.post("/validate-overtime")
async def validate_overtime(
    data: OvertimeValidateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """초과근무 사전 검증 — 스케줄 생성 전 주간 근무시간 초과 여부를 확인합니다.

    Pre-validate weekly overtime before creating a schedule.
    Returns warning info if adding these hours would exceed thresholds.
    """
    return await schedule_service.validate_overtime(
        db,
        organization_id=current_user.organization_id,
        user_id=UUID(data.user_id),
        work_date=data.work_date,
        hours=data.hours,
    )
