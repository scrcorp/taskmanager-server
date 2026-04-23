"""앱 프로필 라우터 — 사용자 프로필 관리 API.

App Profile Router — API endpoints for user profile management.
Provides read and update operations for the current user's profile.
Follows 3-layer architecture: Router → Service → Repository.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.attendance_device import ClockinPinResponse
from app.schemas.user import ProfileResponse, ProfileUpdate
from app.services.attendance_device_service import generate_unique_clockin_pin
from app.services.profile_service import profile_service

router: APIRouter = APIRouter()


@router.get("/profile", response_model=ProfileResponse)
async def get_my_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProfileResponse:
    """내 프로필을 조회합니다.

    Get the current user's profile.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        ProfileResponse: 프로필 정보 (Profile information)
    """
    return await profile_service.get_profile(db, current_user)


@router.put("/profile", response_model=ProfileResponse)
async def update_my_profile(
    data: ProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProfileResponse:
    """내 프로필을 업데이트합니다.

    Update the current user's profile.

    Args:
        data: 업데이트 데이터 (Update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        ProfileResponse: 업데이트된 프로필 정보 (Updated profile information)
    """
    return await profile_service.update_profile(db, current_user, data)


@router.get("/profile/clockin-pin", response_model=ClockinPinResponse)
async def get_my_clockin_pin(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ClockinPinResponse:
    """내 attendance device PIN 을 조회합니다."""
    if current_user.clockin_pin is None:
        current_user.clockin_pin = await generate_unique_clockin_pin(
            db, current_user.organization_id
        )
        await db.commit()
    return ClockinPinResponse(user_id=current_user.id, clockin_pin=current_user.clockin_pin)


@router.post("/profile/clockin-pin/regenerate", response_model=ClockinPinResponse)
async def regenerate_my_clockin_pin(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ClockinPinResponse:
    """내 PIN 을 새 값으로 교체."""
    current_user.clockin_pin = await generate_unique_clockin_pin(
        db, current_user.organization_id
    )
    await db.commit()
    return ClockinPinResponse(user_id=current_user.id, clockin_pin=current_user.clockin_pin)
