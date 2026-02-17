"""앱 인증 라우터 — 앱 회원가입, 로그인, 토큰 갱신, 로그아웃.

App Auth Router — App registration, login, token refresh, and logout endpoints.
Allows staff (level 4) and supervisor (level 3) accounts.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserMeResponse,
)
from app.services.auth_service import auth_service

router: APIRouter = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
async def app_register(
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_organization_id: Annotated[UUID, Header(description="조직 ID 헤더")],
) -> TokenResponse:
    """앱 사용자 회원가입 — 스태프(level 4) 계정 생성.

    App user registration. Creates a staff-level account.
    Organization ID is provided via X-Organization-Id header.
    """
    result: TokenResponse = await auth_service.app_register(
        db, data, x_organization_id
    )
    await db.commit()
    return result


@router.post("/login", response_model=TokenResponse)
async def app_login(
    data: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_organization_id: Annotated[UUID | None, Header(description="조직 ID 헤더")] = None,
) -> TokenResponse:
    """앱 로그인 — 스태프 및 슈퍼바이저 허용.

    App login endpoint. Allows staff and supervisor accounts.
    """
    result: TokenResponse = await auth_service.app_login(
        db, data, x_organization_id
    )
    await db.commit()
    return result


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

    Get the profile of the currently authenticated app user.
    """
    return await auth_service.get_me(db, current_user)
