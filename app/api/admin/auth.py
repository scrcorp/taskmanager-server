"""관리자 인증 라우터 — 관리자 로그인, 토큰 갱신, 로그아웃.

Admin Auth Router — Admin login, token refresh, and logout endpoints.
Staff-level accounts (role level >= 4) are rejected from admin login.
"""

from typing import Annotated

from sqlalchemy import select, func
from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_supervisor
from app.database import get_db
from app.models.organization import Organization
from app.models.user import Role, User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserMeResponse,
)
from app.services.auth_service import auth_service
from app.utils.password import hash_password

router: APIRouter = APIRouter()


@router.post("/login", response_model=TokenResponse)
async def admin_login(
    data: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """관리자 로그인 — 스태프 계정 접근 불가.

    Admin login endpoint. Rejects staff-level accounts (level >= 4).
    """
    result: TokenResponse = await auth_service.admin_login(db, data)
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


@router.post("/setup", response_class=HTMLResponse)
async def admin_setup(
    organization_name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """최초 관리자 계정을 생성합니다.

    Create initial organization and admin account.
    Only works when no organizations exist yet.
    Redirects back to /setup page with result message.
    """
    from app.api.admin.setup import _render

    count = (await db.execute(select(func.count()).select_from(Organization))).scalar() or 0
    if count > 0:
        return _render('<div class="msg err">Setup already completed.</div>')

    # 조직 생성 — Create organization
    org = Organization(name=organization_name)
    db.add(org)
    await db.flush()

    # 기본 역할 4개 생성 — Create 4 default roles
    admin_role: Role | None = None
    for name, level in [("admin", 1), ("manager", 2), ("supervisor", 3), ("staff", 4)]:
        role = Role(organization_id=org.id, name=name, level=level)
        db.add(role)
        if level == 1:
            admin_role = role
    await db.flush()

    assert admin_role is not None

    # 관리자 계정 생성 — Create admin user
    user = User(
        organization_id=org.id,
        role_id=admin_role.id,
        username=username,
        full_name=username,
        password_hash=hash_password(password),
    )
    db.add(user)
    await db.commit()

    return _render(
        f'<div class="msg ok">Done! Organization "{organization_name}" and admin "{username}" created.</div>'
    )


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> UserMeResponse:
    """현재 사용자 프로필 조회.

    Get the profile of the currently authenticated admin user.
    """
    return await auth_service.get_me(db, current_user)
