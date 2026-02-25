"""관리자 인증 라우터 — 관리자 로그인, 초기 설정.

Admin Auth Router — Admin login and initial setup endpoints.
Staff-level accounts (role level >= 4) are rejected from admin login.
Common endpoints (refresh, logout, me) are in app.api.auth.
"""

from typing import Annotated

from sqlalchemy import select, func
from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.organization import Organization
from app.models.user import Role, User
from app.schemas.auth import LoginRequest, TokenResponse
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
    Optionally accepts company_code in body to scope login to a specific org.
    """
    organization_id = await auth_service.resolve_company_code(db, data.company_code)
    result: TokenResponse = await auth_service.admin_login(db, data, organization_id)
    await db.commit()
    return result


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
    for name, level in [("owner", 10), ("general_manager", 20), ("supervisor", 30), ("staff", 40)]:
        role = Role(organization_id=org.id, name=name, level=level)
        db.add(role)
        if level == 10:
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
        f'<div class="msg ok">Done! Organization "{organization_name}" and admin "{username}" created.<br>'
        f'Company Code: <strong>{org.code}</strong></div>'
    )
