"""공통 인증 라우터 — 토큰 갱신, 로그아웃, 프로필 조회.

Common Auth Router — Token refresh, logout, and profile endpoints.
Shared by both admin and app clients.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import RefreshRequest, TokenResponse, UserMeResponse
from app.services.auth_service import auth_service

router: APIRouter = APIRouter()


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    data: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """토큰 갱신 — 리프레시 토큰으로 새 토큰 쌍 발급.

    Refresh token endpoint. Issues a new token pair using a refresh token.
    """
    result: TokenResponse = await auth_service.refresh_tokens(db, data)
    await db.commit()
    return result


@router.post("/logout", status_code=204)
async def logout(
    data: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """로그아웃 — 리프레시 토큰 폐기.

    Logout endpoint. Revokes the given refresh token.
    """
    await auth_service.logout(db, data.refresh_token)
    await db.commit()


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserMeResponse:
    """현재 사용자 프로필 조회.

    Get the profile of the currently authenticated user.
    """
    return await auth_service.get_me(db, current_user)
