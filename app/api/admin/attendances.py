"""관리자 근태 라우터 — 근태 기록 관리 API.

Admin Attendance Router — API endpoints for attendance record management.
Provides list, detail, and correction endpoints for attendance records.
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
    AttendanceCorrectionRequest,
    AttendanceCorrectionResponse,
    AttendanceResponse,
    PaginatedResponse,
)
from app.services.attendance_service import attendance_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_attendances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    work_date: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """근태 기록 목록을 필터링하여 조회합니다.

    List attendance records with optional filters.

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
        dict: 페이지네이션된 근태 목록 (Paginated attendance list)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    attendances, total = await attendance_service.get_attendances(
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
    for a in attendances:
        response: dict = await attendance_service.build_response(db, a)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{attendance_id}", response_model=AttendanceResponse)
async def get_attendance(
    attendance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> dict:
    """근태 기록 상세를 조회합니다 (수정 이력 포함).

    Get attendance record detail with correction history.

    Args:
        attendance_id: 근태 UUID (Attendance UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 근태 상세 + 수정 이력 (Attendance detail with corrections)
    """
    attendance = await attendance_service.get_attendance(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
    )

    response: dict = await attendance_service.build_response(db, attendance)

    # 수정 이력 추가 — Append correction history
    corrections = await attendance_service.get_corrections(db, attendance_id)
    correction_items: list[dict] = []
    for c in corrections:
        correction_items.append(await attendance_service.build_correction_response(db, c))
    response["corrections"] = correction_items

    return response


@router.patch("/{attendance_id}/correct", response_model=AttendanceCorrectionResponse)
async def correct_attendance(
    attendance_id: UUID,
    data: AttendanceCorrectionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """근태 기록을 수정합니다 (GM+ 전용).

    Correct an attendance record field (GM+ only).

    Args:
        attendance_id: 근태 UUID (Attendance UUID)
        data: 수정 요청 데이터 (Correction request data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)

    Returns:
        dict: 생성된 수정 이력 (Created correction record)
    """
    correction = await attendance_service.correct_attendance(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        field_name=data.field_name,
        corrected_value=data.corrected_value,
        reason=data.reason,
        corrected_by=current_user.id,
    )
    await db.commit()

    return await attendance_service.build_correction_response(db, correction)


@router.get("/weekly-summary")
async def get_weekly_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    user_id: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
    week_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """주간 근무시간 요약 — 사용자별 주간 실 근무시간 (총시간 - 휴식).

    Weekly work time summary — net work hours per user.
    """
    return await attendance_service.get_weekly_summary(
        db,
        organization_id=current_user.organization_id,
        user_id=UUID(user_id) if user_id else None,
        store_id=UUID(store_id) if store_id else None,
        week_date=week_date,
    )


@router.get("/overtime-alerts")
async def get_overtime_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: Annotated[str | None, Query()] = None,
    week_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """초과근무 경고 목록 조회 — 주간 근무시간 초과 직원 목록.

    Get overtime alerts — List employees exceeding weekly work hour limits.
    Returns users whose total weekly hours exceed the configured threshold.
    """
    return await attendance_service.get_overtime_alerts(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        week_date=week_date,
    )
