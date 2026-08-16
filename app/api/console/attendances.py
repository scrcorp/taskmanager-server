"""관리자 근태 라우터 — 근태 기록 관리 API.

Admin Attendance Router — API endpoints for attendance record management.
Provides list, detail, and correction endpoints for attendance records.
"""

from datetime import date
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.common import INVALID_DATE_RANGE
from app.api.deps import check_store_access, get_accessible_store_ids, require_permission
from app.database import get_db
from app.models.user import User
from app.services.attendance_export_service import attendance_export_service
from app.utils.download import content_disposition, safe_filename
from app.utils.exceptions import BadRequestError
from app.schemas.common import (
    AttendanceCorrectionRequest,
    AttendanceCorrectionResponse,
    AttendanceCorrectionUpdateRequest,
    AttendanceResponse,
    BreakSessionCreateRequest,
    BreakSessionResponse,
    BreakSessionUpdateRequest,
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
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
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
        date_from: 시작일 필터, 선택 (Optional date range start)
        date_to: 종료일 필터, 선택 (Optional date range end)
        status: 상태 필터, 선택 (Optional status filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 근태 목록 (Paginated attendance list)
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    user_uuid: UUID | None = UUID(user_id) if user_id else None

    # 접근 가능 매장으로 스코프 제한 (owner=None=전체). 특정 store_id 요청 시 접근 검증.
    accessible = await get_accessible_store_ids(db, current_user)
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)

    attendances, total = await attendance_service.get_attendances(
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
        store_ids=accessible,
    )

    # Batch-load breaks for all attendance IDs to avoid N+1 queries
    attendance_ids: list[UUID] = [a.id for a in attendances]
    breaks_map = await attendance_service._load_breaks_map(db, attendance_ids)
    # 수정 이력 개수도 batch 로 — weekly/daily view 에서 "수정됨" 표시용.
    correction_counts = await attendance_service.count_corrections_by_ids(
        db, attendance_ids
    )

    # late_buffer 는 (org, store) 별 1회만 해석해 주입 — 행마다 설정 조회하면 N+1.
    from app.services.attendance_threshold_service import resolve_late_buffer_cached

    lb_cache: dict = {}
    items: list[dict] = []
    for a in attendances:
        late_buffer = await resolve_late_buffer_cached(
            db, lb_cache, organization_id=a.organization_id, store_id=a.store_id
        )
        response: dict = await attendance_service.build_response(
            db, a, breaks=breaks_map.get(a.id, []), late_buffer=late_buffer
        )
        response["correction_count"] = correction_counts.get(a.id, 0)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# NOTE: 고정 경로(/weekly-summary, /overtime-alerts)는 반드시 /{attendance_id}
# 보다 먼저 등록해야 한다 — FastAPI 는 등록 순서로 매칭하므로 뒤에 두면
# "weekly-summary" 가 UUID 파싱에 걸려 422 로 도달 불가(P0-1/P0-2 회귀 방지).


@router.get("/weekly-summary")
async def get_weekly_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    user_id: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
    week_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """주간 근무시간 요약 — 사용자별 주간 실 근무시간 (C1 net).

    Weekly work time summary — net work hours per user.
    """
    store_uuid = UUID(store_id) if store_id else None
    accessible = await get_accessible_store_ids(db, current_user)
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)
    return await attendance_service.get_weekly_summary(
        db,
        organization_id=current_user.organization_id,
        user_id=UUID(user_id) if user_id else None,
        store_id=store_uuid,
        week_date=week_date,
        store_ids=accessible,
    )


@router.get("/overtime-alerts")
async def get_overtime_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: Annotated[str | None, Query()] = None,
    week_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """초과근무 경고 목록 조회 — 주간 순 근무시간 초과 직원 목록.

    Get overtime alerts — List employees exceeding weekly work hour limits.
    Returns users whose weekly net hours exceed the per-store threshold.
    """
    store_uuid = UUID(store_id) if store_id else None
    accessible = await get_accessible_store_ids(db, current_user)
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)
    return await attendance_service.get_overtime_alerts(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        week_date=week_date,
        store_ids=accessible,
    )


@router.get("/export")
async def export_attendances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    store_id: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """근태 기록을 xlsx 로 내보냅니다 — 기간 필수, 매장 선택.

    Export attendance records to xlsx (time records only — no pay columns).
    권한/스코프는 목록 GET 과 동일: schedules:read + 접근 가능 매장 제한.
    """
    if date_from > date_to:
        raise INVALID_DATE_RANGE()

    store_uuid: UUID | None = UUID(store_id) if store_id else None
    accessible = await get_accessible_store_ids(db, current_user)
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)

    excel_bytes: bytes = await attendance_export_service.build_export(
        db,
        organization_id=current_user.organization_id,
        date_from=date_from,
        date_to=date_to,
        store_id=store_uuid,
        store_ids=accessible,
    )
    filename: str = safe_filename(f"attendance_{date_from}_{date_to}.xlsx")
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)},
    )


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
    response["correction_count"] = len(correction_items)

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

    return await attendance_service.build_correction_response(db, correction)


@router.post(
    "/{attendance_id}/confirm-auto-clockout",
    response_model=AttendanceResponse,
)
async def confirm_auto_clockout(
    attendance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """자동퇴근(auto_clocked_out) 건을 매니저가 확인 처리합니다 (L6 일상 플로우).

    Confirm an auto clocked-out attendance (daily/next-day manager check).
    Sets auto_clock_out_confirmed_by/at — the basis for payroll close gate #1
    (zero unconfirmed auto clock-outs).

    동작:
        - 'auto_clocked_out' anomaly 가 없는 record → 400
        - 이미 확인된 record → **no-op 멱등 성공** (최초 확인자/시각 보존)
        - clock_out 시각을 correction 으로 고치면 이 endpoint 없이도 자동 확인됨
          (corrected == human-verified)
    """
    attendance = await attendance_service.confirm_auto_clock_out(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        confirmed_by=current_user.id,
    )
    return await attendance_service.build_response(db, attendance)


@router.post(
    "/{attendance_id}/confirm-early-clockin",
    response_model=AttendanceResponse,
)
async def confirm_early_clockin(
    attendance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """조기 출근 강행(early_clock_in_override) 건을 매니저가 확인 처리합니다.

    Confirm an early clock-in override. Sets early_clock_in_confirmed_by/at —
    the basis for the payroll close gate (zero unconfirmed early clock-ins).

    동작:
        - override anomaly 가 없는 record → 400
        - 이미 확인된 record → **no-op 멱등 성공** (최초 확인자/시각 보존)
        - 매니저 대행으로 찍힌 건은 생성 시점에 이미 확인 상태다
    """
    attendance = await attendance_service.confirm_early_clock_in(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        confirmed_by=current_user.id,
    )
    return await attendance_service.build_response(db, attendance)


@router.patch(
    "/{attendance_id}/corrections/{correction_id}",
    response_model=AttendanceCorrectionResponse,
)
async def update_correction_reason(
    attendance_id: UUID,
    correction_id: UUID,
    data: AttendanceCorrectionUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """기존 correction 의 reason 을 수정 (History 인라인 편집).

    Update reason on an existing correction record. Reason 만 변경 가능,
    field_name/value 는 새 correction 으로 처리해야 함.
    """
    correction = await attendance_service.update_correction_reason(
        db,
        attendance_id=attendance_id,
        correction_id=correction_id,
        organization_id=current_user.organization_id,
        reason=data.reason,
    )
    return await attendance_service.build_correction_response(db, correction)


@router.post(
    "/{attendance_id}/breaks",
    response_model=BreakSessionResponse,
    status_code=201,
)
async def add_break_session(
    attendance_id: UUID,
    data: BreakSessionCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """admin 이 새 break 세션을 attendance 에 추가 (GM+ 전용)."""
    new_break = await attendance_service.add_break(
        db,
        attendance_id=attendance_id,
        organization_id=current_user.organization_id,
        started_at=data.started_at,
        ended_at=data.ended_at,
        break_type=data.break_type,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return {
        "id": str(new_break.id),
        "started_at": new_break.started_at,
        "ended_at": new_break.ended_at,
        "break_type": new_break.break_type,
        "duration_minutes": new_break.duration_minutes,
    }


@router.patch(
    "/{attendance_id}/breaks/{break_id}",
    response_model=BreakSessionResponse,
)
async def update_break_session(
    attendance_id: UUID,
    break_id: UUID,
    data: BreakSessionUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """admin 이 기존 break 세션을 수정 (GM+ 전용)."""
    target = await attendance_service.update_break(
        db,
        attendance_id=attendance_id,
        break_id=break_id,
        organization_id=current_user.organization_id,
        started_at=data.started_at,
        ended_at=data.ended_at,
        break_type=data.break_type,
        clear_ended_at=data.clear_ended_at,
        reason=data.reason,
        by_user_id=current_user.id,
    )
    return {
        "id": str(target.id),
        "started_at": target.started_at,
        "ended_at": target.ended_at,
        "break_type": target.break_type,
        "duration_minutes": target.duration_minutes,
    }


@router.delete(
    "/{attendance_id}/breaks/{break_id}",
    status_code=204,
)
async def delete_break_session(
    attendance_id: UUID,
    break_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
    reason: str | None = None,
) -> None:
    """admin 이 break 세션을 삭제 (GM+ 전용).

    사유는 선택 — DELETE 라 body 대신 query 로 받는다 (`?reason=...`).
    """
    await attendance_service.delete_break(
        db,
        attendance_id=attendance_id,
        break_id=break_id,
        organization_id=current_user.organization_id,
        reason=reason,
        by_user_id=current_user.id,
    )
