"""관리자 프로필 라우터 — admin 사용자의 본인 프로필/알림 설정.

Admin user's own profile/alert settings.
ProfileService 는 admin/app 양쪽이 공유하지만 admin 의 별도 권한 없는
self-service 엔드포인트로 노출하기 위해 라우터를 분리한다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.user import (
    AlertPreferencesResponse,
    AlertPreferencesUpdate,
)
from app.services.profile_service import profile_service

router: APIRouter = APIRouter()


@router.get("/alert-preferences", response_model=AlertPreferencesResponse)
async def get_my_alert_preferences(
    current_user: Annotated[User, Depends(get_current_user)],
) -> AlertPreferencesResponse:
    """내 알림 선호 + 카테고리 메타 조회. 클라가 그대로 렌더 가능한 응답."""
    return await profile_service.get_alert_preferences(current_user)


@router.put("/alert-preferences", response_model=AlertPreferencesResponse)
async def update_my_alert_preferences(
    data: AlertPreferencesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AlertPreferencesResponse:
    """내 알림 선호 부분 업데이트."""
    return await profile_service.update_alert_preferences(db, current_user, data)
