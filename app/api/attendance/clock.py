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
    )
    return await attendance_service.build_response(db, attendance)


@router.post("/clock-in")
async def clock_in(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(
        db, device, data.pin, data.user_id, "clock_in",
        schedule_id=data.schedule_id,
    )


@router.post("/clock-out")
async def clock_out(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(
        db, device, data.pin, data.user_id, "clock_out", reason=data.reason,
    )


@router.post("/break-start")
async def break_start(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(
        db, device, data.pin, data.user_id, "break_start", break_type=data.break_type
    )


@router.post("/break-end")
async def break_end(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(db, device, data.pin, data.user_id, "break_end")
