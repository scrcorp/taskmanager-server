"""앱 근태 라우터 — 내 근태 기록 API.

App Attendance Router — API endpoints for user's own attendance records.
Provides QR scan, today's attendance, and attendance history.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    AttendanceResponse,
    AttendanceScanRequest,
    PaginatedResponse,
)
from app.services.attendance_service import attendance_service

router: APIRouter = APIRouter()


@router.post("/scan", response_model=AttendanceResponse)
async def scan_attendance(
    data: AttendanceScanRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """QR 코드를 스캔하여 출퇴근/휴식을 기록합니다.

    Scan a QR code to record clock-in, break, or clock-out.

    Args:
        data: QR 스캔 요청 데이터 (QR scan request data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 업데이트된 근태 기록 (Updated attendance record)
    """
    attendance = await attendance_service.scan(
        db,
        qr_code_str=data.qr_code,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        action=data.action,
        client_timezone=data.timezone,
        location=data.location,
    )
    await db.commit()

    return await attendance_service.build_response(db, attendance)


@router.get("/today", response_model=AttendanceResponse | None)
async def get_my_today_attendance(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict | None:
    """오늘 내 근태 기록을 조회합니다.

    Get today's attendance record for the current user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict | None: 오늘 근태 기록 또는 None (Today's attendance or None)
    """
    from datetime import timezone, datetime

    today: date = datetime.now(timezone.utc).date()

    from app.repositories.attendance_repository import attendance_repository

    attendance = await attendance_repository.get_user_today(db, current_user.id, today)

    if attendance is None:
        return None

    return await attendance_service.build_response(db, attendance)


@router.get("", response_model=PaginatedResponse)
async def list_my_attendances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내 근태 기록 목록을 조회합니다.

    List my attendance records with optional date filter.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        work_date: 근무일 필터, 선택 (Optional work date filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 내 근태 목록 (Paginated my attendance list)
    """
    from app.repositories.attendance_repository import attendance_repository

    attendances, total = await attendance_repository.get_user_attendances(
        db,
        user_id=current_user.id,
        work_date=work_date,
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
