"""앱 인증 라우터 — 앱 회원가입, 로그인.

App Auth Router — App registration and login endpoints.
Allows staff (level 4) and supervisor (level 3) accounts.
Uses company_code in request body to identify organization.
Common endpoints (refresh, logout, me) are in app.api.auth.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.services.auth_service import auth_service

router: APIRouter = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
async def app_register(
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """앱 사용자 회원가입 — 스태프(level 4) 계정 생성.

    App user registration. Creates a staff-level account.
    Organization is identified via company_code in request body.
    """
    organization_id = await auth_service.resolve_company_code(db, data.company_code)
    result: TokenResponse = await auth_service.app_register(
        db, data, organization_id
    )
    await db.commit()
    return result


@router.post("/login", response_model=TokenResponse)
async def app_login(
    data: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """앱 로그인 — 스태프 및 슈퍼바이저 허용.

    App login endpoint. Allows staff and supervisor accounts.
    Optionally accepts company_code in body to scope login to a specific org.
    """
    organization_id = await auth_service.resolve_company_code(db, data.company_code)
    result: TokenResponse = await auth_service.app_login(
        db, data, organization_id
    )
    await db.commit()
    return result
