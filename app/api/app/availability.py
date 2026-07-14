"""앱/셀프 근무가능시간 라우터 — 본인 조회(항상) + 본인 저장(최초 1회 게이트).

JWT 전용(권한 코드 없음). 최초 1회(미설정)만 셀프 저장 허용 — history 존재 시 매니저만.
스태프 앱은 조회만; 저장은 별도 셀프 입력 페이지가 이 PUT 을 호출한다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.availability import AvailabilityWeekUpdate, MyAvailabilityOut
from app.services.availability_service import availability_service

router: APIRouter = APIRouter()


@router.get("/availability", response_model=MyAvailabilityOut)
async def get_my_availability(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MyAvailabilityOut:
    return await availability_service.get_mine(db, current_user)


@router.put("/availability", response_model=MyAvailabilityOut)
async def update_my_availability(
    data: AvailabilityWeekUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MyAvailabilityOut:
    return await availability_service.update_mine(db, current_user, data.days)
