"""앱 인증 라우터 — 앱 회원가입, 로그인.

App Auth Router — App registration and login endpoints.
Allows staff (level 4) and supervisor (level 3) accounts.
Uses company_code in request body to identify organization.
Common endpoints (refresh, logout, me) are in app.api.auth.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.url_encoding import decode_uuid
from app.database import get_db
from app.api.deps import get_current_user
from app.models.organization import Organization, Store
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
from app.services.storage_service import storage_service

router: APIRouter = APIRouter()


@router.get("/stores/by-code/{encoded}")
async def get_store_by_code(
    encoded: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """공개 매장 조회 — 가입 링크 진입점 (No auth).

    Public store lookup for `hermesops.site/join/{encoded}` signup pages.
    Decodes the base64url store_id, returns store/organization info plus
    cover photos resolved to URLs.

    Errors:
        404 invalid_link    — encoded payload is malformed or wrong length
        404 store_not_found — store deleted or doesn't exist
        404 signups_paused  — store has accepting_signups=false
    """
    try:
        store_id = decode_uuid(encoded)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"code": "invalid_link", "message": "Signup link is malformed."},
        )

    result = await db.execute(
        select(Store, Organization)
        .join(Organization, Store.organization_id == Organization.id)
        .where(Store.id == store_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "store_not_found", "message": "Store does not exist."},
        )
    store, org = row

    if not store.is_active or store.deleted_at is not None:
        raise HTTPException(
            status_code=404,
            detail={"code": "store_not_found", "message": "Store is no longer available."},
        )

    if not store.accepting_signups:
        raise HTTPException(
            status_code=404,
            detail={"code": "signups_paused", "message": "This store is not accepting new sign-ups right now."},
        )

    cover_photos = []
    for photo in (store.cover_photos or []):
        url = storage_service.resolve_url(photo.get("key"))
        if url is None:
            continue
        cover_photos.append({
            "url": url,
            "is_primary": bool(photo.get("is_primary", False)),
        })

    return {
        "store": {
            "id": str(store.id),
            "name": store.name,
            "address": store.address,
            "cover_photos": cover_photos,
        },
        "organization": {
            "name": org.name,
            "company_code": org.code,
        },
    }


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


class _DirectSignupBody(BaseModel):
    """매장 다이렉트 가입 (지원자 단계 없이 즉시 staff 등록).

    공개 페이지 `/direct/{encoded}` 진입점. encoded store_id로 매장/조직을 식별하고
    바로 users 생성한다. 기존 회원가입(`/register`)과 동일하게 staff 권한 부여.

    공개 가입(`/applications/submit`)과의 차이: 폼/지원자 단계 없음, 즉시 로그인 가능.
    """

    encoded: str
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    full_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    verification_token: str


@router.post("/direct-signup", response_model=TokenResponse, status_code=201)
async def app_direct_signup(
    data: _DirectSignupBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """매장 다이렉트 가입 — 지원자 단계 건너뛰고 즉시 staff 계정 생성.

    encoded(base64url 인코딩된 store_id)만으로 매장/조직 식별. 폼 검증 없음.
    """
    try:
        store_id = decode_uuid(data.encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})

    # 매장 + 조직 조회 (cover photos 등은 안 가져옴)
    store_res = await db.execute(
        select(Store, Organization).join(Organization, Organization.id == Store.organization_id)
        .where(Store.id == store_id, Store.deleted_at.is_(None))
    )
    row = store_res.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    store, org = row
    if not store.accepting_signups:
        raise HTTPException(status_code=404, detail={"code": "signups_paused"})

    # 기존 register 흐름 위임 — RegisterRequest로 변환
    register_req = RegisterRequest(
        username=data.username,
        password=data.password,
        full_name=data.full_name,
        email=data.email,
        company_code=org.code,
        verification_token=data.verification_token,
        store_ids=[str(store_id)],
    )
    return await auth_service.app_register(db, register_req, org.id)


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
