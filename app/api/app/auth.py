"""앱 인증 라우터 — 앱 회원가입, 로그인.

App Auth Router — App registration and login endpoints.
Allows staff (level 4) and supervisor (level 3) accounts.
Uses company_code in request body to identify organization.
Common endpoints (refresh, logout, me) are in app.api.auth.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.repositories.store_repository import store_repository
from app.schemas.email_verification import (
    SendVerificationCodeRequest,
    VerifyEmailCodeRequest,
    ConfirmEmailRequest,
)
from app.services.auth_service import auth_service
from app.services.email_verification_service import email_verification_service

router: APIRouter = APIRouter()


@router.get("/stores")
async def get_stores_by_company_code(
    company_code: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """회사 코드로 매장 목록 조회 — 인증 불필요.

    Get active stores for an organization by company code.
    Used during registration for store selection. No auth required.
    """
    organization_id = await auth_service.resolve_company_code(db, company_code)
    stores = await store_repository.get_by_org(db, organization_id)
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "address": s.address,
        }
        for s in stores
        if s.is_active and s.deleted_at is None
    ]


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
    return await auth_service.app_register(db, data, organization_id)


@router.post("/login", response_model=TokenResponse)
async def app_login(
    data: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """앱 로그인 — 스태프 및 슈퍼바이저 허용.

    App login endpoint. Allows staff and supervisor accounts.
    Optionally accepts company_code in body to scope login to a specific org.
    """
    from app.api.utils import get_session_info

    organization_id = await auth_service.resolve_company_code(db, data.company_code)
    return await auth_service.app_login(
        db, data, organization_id,
        **get_session_info(request),
    )


@router.post("/send-verification-code")
async def send_verification_code(
    data: SendVerificationCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """인증코드 발송 — 이메일로 6자리 코드 전송.

    Send a 6-digit verification code to the given email.
    Rate limited: 60 seconds between requests for the same email.
    """
    return await email_verification_service.send_code(db, data.email, data.purpose)


@router.post("/verify-email-code")
async def verify_email_code(
    data: VerifyEmailCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """인증코드 검증 — 코드 확인 후 verification_token 발급.

    Verify a 6-digit code. Returns a verification_token on success,
    which must be included in the registration request.
    """
    return await email_verification_service.verify_code(db, data.email, data.code)


@router.post("/confirm-email")
async def confirm_email(
    data: ConfirmEmailRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """로그인 후 이메일 인증 — 기존 사용자가 이메일을 인증.

    Post-login email verification for existing users.
    Updates users.email and sets email_verified=True.
    """
    return await email_verification_service.confirm_email(
        db, current_user, data.email, data.code
    )
