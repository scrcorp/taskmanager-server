"""Attendance device PIN 단독 식별 라우터 (Phase 3).

직원 clock 흐름 PIN-first kiosk 의 entry point. PIN 6자리 → 본인 식별 +
오늘 attendance status. manage 모드 진입 흐름은 Phase 6 에서 별도.

`/api/v1/attendance` 하위에 mount.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.schemas.attendance_device import (
    IdentifyByPinCurrentBreak,
    IdentifyByPinRequest,
    IdentifyByPinResponse,
)
from app.services.attendance_device_service import attendance_device_service


router: APIRouter = APIRouter()


@router.post("/identify-by-pin", response_model=IdentifyByPinResponse)
async def identify_by_pin(
    data: IdentifyByPinRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IdentifyByPinResponse:
    """PIN 단독으로 user 식별 + 오늘 attendance context 반환.

    - PIN 형식 위반 (4~6자리 아님) → 422 (Pydantic) 또는 400 (service)
    - PIN 매치 없음 / 비활성 / 삭제됨 → 400 'Invalid PIN'
    - device.store_id None → today_status=None (식별만)
    - 정상 → user 정보 + today_status + (on_break 시) current_break + scheduled_end
    """
    ctx = await attendance_device_service.identify_user_by_pin(db, data.pin, device)
    current_break_payload: IdentifyByPinCurrentBreak | None = None
    if ctx.current_break is not None:
        current_break_payload = IdentifyByPinCurrentBreak(
            break_type=ctx.current_break["break_type"],
            started_at=ctx.current_break["started_at"],
        )
    return IdentifyByPinResponse(
        user_id=ctx.user.id,
        user_name=ctx.user.full_name or ctx.user.username,
        today_status=ctx.today_status,
        current_break=current_break_payload,
        scheduled_end=ctx.scheduled_end,
    )
