"""Attendance device clock 동작 라우터 — clock-in/out, break-start/end.

`/api/v1/attendance` 하위에 mount.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.schemas.attendance_device import ClockActionRequest
from app.services.attendance_device_service import attendance_device_service
from app.services.attendance_service import attendance_service


router: APIRouter = APIRouter()


async def _perform_action(
    db: AsyncSession,
    device: AttendanceDevice,
    pin: str,
    user_id: uuid.UUID,
    action: str,
    break_type: str | None = None,
    reason: str | None = None,
    schedule_id: uuid.UUID | None = None,
    walk_in: bool = False,
    allow_overlap: bool = False,
    early_clock_in_requested_by: uuid.UUID | None = None,
) -> dict:
    attendance = await attendance_device_service.perform_clock_action(
        db,
        device=device,
        pin=pin,
        action=action,
        user_id=user_id,
        break_type=break_type,
        reason=reason,
        schedule_id=schedule_id,
        walk_in=walk_in,
        allow_overlap=allow_overlap,
        early_clock_in_requested_by=early_clock_in_requested_by,
    )
    response = await attendance_service.build_response(db, attendance)
    # 겹침 clock-in 성공 시에만 붙는 키. **표시 문구는 넣지 않는다** — 앱이 l10n 으로
    # "You are clocked in to two shifts." 안내를 조립한다. 겹치지 않으면 키 자체가 없다.
    # (서비스가 transient 속성으로 넘기고 라우터가 합친다 — tip_service 의 `_schedule_loaded`
    #  와 같은 방식. Attendance 응답 스키마에 겹침 개념을 넣지 않기 위함.)
    overlap = getattr(attendance, "_overlap_info", None)
    if overlap is not None:
        response["overlap"] = overlap
    return response


@router.post("/clock-in")
async def clock_in(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # reason 은 조기 출근 사유 — 예정 시작보다 이르면 필수 (early clock-in override).
    # early_clock_in_requested_by 는 그 사유의 **대상자 식별자**(D9). 표시는 계속
    # reason 문자열이 하고, 이 값은 동명이인·개명 추적을 위한 식별 전용이다.
    # 구버전 HTMA 는 보내지 않는다 → None 저장(정상).
    return await _perform_action(
        db, device, data.pin, data.user_id, "clock_in",
        reason=data.reason,
        schedule_id=data.schedule_id,
        walk_in=data.walk_in,
        allow_overlap=data.allow_overlap,
        early_clock_in_requested_by=data.early_clock_in_requested_by,
    )


@router.post("/clock-out")
async def clock_out(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # schedule_id 는 겹침(D15) 상태에서 어느 shift 를 닫는지 지목한다. 미지정이면
    # 기존처럼 첫 번째 열린 row — 구버전 HTMA 는 보내지 않으므로 동작이 그대로다.
    return await _perform_action(
        db, device, data.pin, data.user_id, "clock_out", reason=data.reason,
        schedule_id=data.schedule_id,
    )


@router.post("/break-start")
async def break_start(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(
        db, device, data.pin, data.user_id, "break_start", break_type=data.break_type,
        schedule_id=data.schedule_id,
    )


@router.post("/break-end")
async def break_end(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # reason 은 break 초과 사유 — unpaid_meal 35분 이상이면 필수 (break_end_policy).
    return await _perform_action(
        db, device, data.pin, data.user_id, "break_end", reason=data.reason,
        schedule_id=data.schedule_id,
    )
