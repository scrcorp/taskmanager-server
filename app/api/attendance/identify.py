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
    """PIN 단독으로 user 식별 + 오늘 attendance status 반환.

    - PIN 형식 위반 → 422 (Pydantic) 또는 400 (service 에서)
    - PIN 매치 없음 / 비활성 / 삭제됨 → 400 'Invalid PIN'
    - device.store_id None → today_status=None (식별만)
    - 정상 → user 정보 + today_status (스케줄 없으면 None)
    """
    user, today_status = await attendance_device_service.identify_user_by_pin(
        db, data.pin, device
    )
    return IdentifyByPinResponse(
        user_id=user.id,
        user_name=user.full_name or user.username,
        today_status=today_status,
    )
