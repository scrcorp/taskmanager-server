"""Attendance device 명단 조회 라우터 — 매장 Manager/SV 목록.

`/api/v1/attendance` 하위에 mount.

조기 출근 사유의 "누가 일찍 오라고 했나"(D8/D9) 를 **실제 사람으로** 고르게 하려면
키오스크가 그 매장의 Manager/SV 명단을 알아야 한다. 그 목적 하나만 담당한다.

개인정보 관점의 설계 (전부 의도된 제약):
  - **GET 이 아니라 POST + PIN 게이트.** 매니저 명단은 사회공학 표적 정보라
    device token 만으로 열지 않고 "PIN 으로 식별된 직원" 에게만 연다
    (`tip-entry/eligible-receivers` 선례).
  - 응답 필드는 `user_id / full_name / role_name / role_priority` 뿐.
    연락처·PIN·시급은 어떤 경우에도 싣지 않는다.
  - 노출 범위는 Manager/SV(+Owner) 로 한정한다 — 전체 직원 명단은 내리지 않는다.
  - 앱은 **사유 단계에 들어갈 때만** 호출한다(상시 프리페치·세션 캐시 금지).
  - 서버 로그에 이름을 남기지 않는다.

`store-employees`(전 직원 이름)가 tip.py 에 얹혀 있는 게 이미 어색하므로 새 것을
거기 또 얹지 않는다. 그 엔드포인트가 이보다 느슨하다는 사실은 별건 리스크다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.core.error_codes.attendance import DEVICE_NO_STORE
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.schemas.attendance_device import StoreManagerListRequest, StoreManagerOption
from app.services.attendance_device_service import attendance_device_service


router: APIRouter = APIRouter()


@router.post("/store-managers")
async def device_store_managers(
    data: StoreManagerListRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[StoreManagerOption]:
    """이 매장의 Manager/SV 명단 — 조기 출근 사유의 대상자 선택용.

    빈 목록도 정상(200 []). 앱은 명단이 비었거나 이 호출이 실패해도 "Someone else"
    자유 입력으로 사유를 제출할 수 있어야 한다 — 명단은 정확도를 높이는 보조지
    통과 조건이 아니다.
    """
    if device.store_id is None:
        raise DEVICE_NO_STORE()

    user = await attendance_device_service.verify_user_pin(
        db, data.user_id, data.pin, device.organization_id,
    )
    rows = await attendance_device_service.list_store_managers(
        db,
        organization_id=device.organization_id,
        store_id=device.store_id,
        asking_user_id=user.id,
    )
    return [StoreManagerOption(**r) for r in rows]
