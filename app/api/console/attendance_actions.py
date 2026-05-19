"""콘솔 attendance 액션 라우터 — 의미 있는 단위로만 attendance 상태 변경.

Console attendance action endpoints. Each route corresponds to a semantic
state machine transition (Clock In/Out, Start/End Break, etc.). All routes
go through `attendance_action_service` which enforces invariants — direct
status edits via `correct_attendance` 는 차단된다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    AttendanceBreakStartRequest,
    AttendanceClockActionRequest,
    AttendanceReasonOnlyRequest,
    AttendanceResponse,
)
from app.services.attendance_action_service import attendance_action_service
from app.services.attendance_service import attendance_service

router: APIRouter = APIRouter()


async def _build(db: AsyncSession, attendance) -> dict:
    """공통 응답 빌드 — Attendance + corrections + correction_count."""
    response: dict = await attendance_service.build_response(db, attendance)
    corrections = await attendance_service.get_corrections(db, attendance.id)
    response["corrections"] = [
        await attendance_service.build_correction_response(db, c) for c in corrections
    ]
    response["correction_count"] = len(response["corrections"])
    return response


@router.post(
    "/{attendance_id}/actions/clock-in",
    response_model=AttendanceResponse,
)
async def clock_in_action(
    attendance_id: UUID,
    data: AttendanceClockActionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """Clock-in 기록 + 스케줄 기준 working/late 자동 판정."""
    attendance = await attendance_action_service.clock_in(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        at=data.at,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/clock-out",
    response_model=AttendanceResponse,
)
async def clock_out_action(
    attendance_id: UUID,
    data: AttendanceClockActionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """Clock-out + 진행중 break 자동 종료 + status=clocked_out."""
    attendance = await attendance_action_service.clock_out(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        at=data.at,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/start-break",
    response_model=AttendanceResponse,
)
async def start_break_action(
    attendance_id: UUID,
    data: AttendanceBreakStartRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """Break 시작 + status=on_break. working/late 일 때만."""
    attendance = await attendance_action_service.start_break(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        at=data.at,
        break_type=data.break_type,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/end-break",
    response_model=AttendanceResponse,
)
async def end_break_action(
    attendance_id: UUID,
    data: AttendanceClockActionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """진행중 break 종료 + status=working."""
    attendance = await attendance_action_service.end_break(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        at=data.at,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/mark-no-show",
    response_model=AttendanceResponse,
)
async def mark_no_show_action(
    attendance_id: UUID,
    data: AttendanceReasonOnlyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """status=no_show. 시간 기록이 있으면 거부."""
    attendance = await attendance_action_service.mark_no_show(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/cancel",
    response_model=AttendanceResponse,
)
async def cancel_action(
    attendance_id: UUID,
    data: AttendanceReasonOnlyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """status=cancelled. 출근 전(clock_in 없음) shift 만 가능."""
    attendance = await attendance_action_service.cancel(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)


@router.post(
    "/{attendance_id}/actions/reopen",
    response_model=AttendanceResponse,
)
async def reopen_action(
    attendance_id: UUID,
    data: AttendanceReasonOnlyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """이전 상태로 되돌리기 (clocked_out/no_show/cancelled → 이전).

    - clocked_out → clock_out 제거, 진행중 break 있으면 on_break, 아니면 working
    - no_show → upcoming (anomaly no_show 제거)
    - cancelled → upcoming
    """
    attendance = await attendance_action_service.reopen(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return await _build(db, attendance)
