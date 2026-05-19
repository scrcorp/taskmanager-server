"""FastAPI 의존성 주입 모듈 — 인증 및 권한 검사.

FastAPI dependency injection module — Authentication and authorization.
Provides reusable dependencies for extracting the current user from JWT
and enforcing permission-based access control on API endpoints.
"""

from datetime import datetime, timezone
from typing import Annotated, Callable, Awaitable
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.utils.jwt import decode_token
from app.models.user import User
from app.models.attendance_device import AttendanceDevice
from app.repositories.permission_repository import permission_repository
from app.core.permissions import (
    SUPER_OWNER_ONLY,
    hide_cost_for_priority,
    is_gm_plus,
    is_owner,
    is_super_owner,
)

security: HTTPBearer = HTTPBearer()
device_security: HTTPBearer = HTTPBearer(auto_error=False)


async def get_current_attendance_device(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(device_security)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Attendance Device 토큰으로 현재 기기를 인증.

    평문 토큰 → sha256 해시 → attendance_devices 매칭. revoke 된 기기는 row 가
    없으므로 자동으로 401. JWT 와 별개의 인증 스코프이며, 매 호출마다
    last_seen_at 갱신.
    """
    from app.services.attendance_device_service import attendance_device_service

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Attendance device token required",
        )
    device = await attendance_device_service.get_by_token(db, credentials.credentials)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid attendance device token",
        )
    await attendance_device_service.touch_last_seen(db, device)
    await db.commit()
    return device


async def get_current_attendance_admin_session(
    request: Request,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Kiosk 관리자 모드 세션 검증.

    `X-Admin-Session` 헤더로 전달된 토큰을 in-memory 세션 캐시에서 조회.
    device token 과 같은 device 에서 발급되었는지 검증한다.
    매니저 user 가 비활성/삭제됐거나 더 이상 store 의 매니저가 아니면 거부.
    반환: (device, admin_session, manager_user)
    """
    from app.core.attendance_admin_session import get_session
    from app.core.permissions import is_owner, is_sv_plus
    from app.models.user_store import UserStore

    token = request.headers.get("X-Admin-Session") or request.headers.get("x-admin-session")
    session = get_session(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session required or expired",
        )
    if session.device_id != device.id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session does not match this device",
        )
    if device.store_id is None or session.store_id != device.store_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device store has changed; please re-enter manager mode",
        )

    # 매니저 user 재검증 (활성 + 권한)
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(
            User.id == session.manager_user_id,
            User.organization_id == device.organization_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    manager = result.scalar_one_or_none()
    if manager is None or manager.role is None or not is_sv_plus(manager):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager no longer authorized",
        )
    # Owner 는 모든 매장 관리. 그 외는 user_stores.is_manager 확인.
    if not is_owner(manager):
        us = await db.execute(
            select(UserStore).where(
                UserStore.user_id == manager.id,
                UserStore.store_id == device.store_id,
                UserStore.is_manager.is_(True),
            )
        )
        if us.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Manager has no permission for this store",
            )

    return device, session, manager


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """JWT 토큰에서 현재 인증된 사용자를 추출합니다."""
    try:
        payload: dict = decode_token(credentials.credentials)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except HTTPException:
        raise
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == UUID(user_id))
    )
    user: User | None = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # 비밀번호 변경 후 발급된 토큰인지 확인 — Reject tokens issued before password change
    # 2초 여유: 비밀번호 변경 시 password_changed_at과 새 토큰 iat이 같은 초에 생성될 수 있음
    if user.password_changed_at:
        iat = payload.get("iat")
        if iat:
            from datetime import timedelta
            issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
            if issued_at < user.password_changed_at - timedelta(seconds=2):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Password changed, please re-login")

    return user


async def get_user_permissions(db: AsyncSession, role_id: UUID) -> set[str]:
    """role_id → permission codes set 조회."""
    return await permission_repository.get_permissions_by_role_id(db, role_id)


def require_permission(*permission_codes: str) -> Callable[..., Awaitable[User]]:
    """Permission 기반 권한 검사 의존성 팩토리.

    지정된 모든 permission code를 가지고 있어야 접근 허용.
    """
    async def _check(
        current_user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        # Super Owner 는 항상 통과 (모든 권한 보유).
        if is_super_owner(current_user):
            return current_user
        # Owner 는 super_owner 전용 permission 이 아닌 경우에만 bypass.
        # super_owner 전용(org:delete / owner:assign / super_owner:transfer)
        # 은 Owner 라도 일반 check 로 진입 → role_permissions 에 없으면 403.
        if is_owner(current_user) and not any(c in SUPER_OWNER_ONLY for c in permission_codes):
            return current_user
        user_perms = await get_user_permissions(db, current_user.role_id)
        for code in permission_codes:
            if code not in user_perms:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: {code}",
                )
        return current_user
    return _check


def hide_cost_for(user: User) -> bool:
    """SV 이상이면 cost(hourly_rate) 정보 숨김."""
    return hide_cost_for_priority(user.role.priority if user.role else 999)


def scrub_cost_fields(obj: object, fields: tuple[str, ...] = ("hourly_rate", "default_hourly_rate", "effective_hourly_rate")) -> None:
    """response 객체의 cost 관련 필드를 None으로 설정 (SV/Staff용)."""
    for f in fields:
        if hasattr(obj, f):
            setattr(obj, f, None)


async def get_accessible_store_ids(
    db: AsyncSession, user: User
) -> list[UUID] | None:
    """사용자가 접근 가능한 매장 ID 목록 (admin용).

    None = full access (Owner).
    GM: is_manager=true 매장 (관리 책임 매장만).
    SV/Staff: user_stores에 등록된 모든 매장 (배정 매장 전체).
    """
    if is_owner(user):
        return None
    from app.repositories.user_repository import user_repository
    if is_gm_plus(user):
        return await user_repository.get_managed_store_ids(db, user.id)
    return await user_repository.get_user_store_ids(db, user.id)


async def get_work_store_ids(
    db: AsyncSession, user: User
) -> list[UUID] | None:
    """사용자의 근무매장 ID 목록 (staff app용).

    None = full access (Owner). List = 모든 배정 매장.
    """
    if is_owner(user):
        return None
    from app.repositories.user_repository import user_repository
    return await user_repository.get_work_store_ids(db, user.id)


async def check_store_access(
    db: AsyncSession, user: User, store_id: UUID
) -> None:
    """사용자가 특정 매장에 접근 가능한지 확인합니다. 불가 시 403 발생."""
    accessible = await get_accessible_store_ids(db, user)
    if accessible is not None and store_id not in accessible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this store",
        )
