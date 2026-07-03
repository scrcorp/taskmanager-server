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


async def get_current_attendance_manage_session(
    request: Request,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Kiosk 관리자 모드 세션 검증.

    `X-Manage-Session` 헤더로 전달된 토큰을 in-memory 세션 캐시에서 조회.
    device token 과 같은 device 에서 발급되었는지 검증한다.
    매니저 user 가 비활성/삭제됐거나 더 이상 store 의 매니저가 아니면 거부.
    반환: (device, manage_session, manager_user)
    """
    from app.core.attendance_manage_session import get_session
    from app.core.permissions import is_owner, is_sv_plus
    from app.models.user_store import UserStore

    token = request.headers.get("X-Manage-Session") or request.headers.get("x-manage-session")
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

    # [Model B] JWT 의 org 컨텍스트를 사용자의 실제 멤버십과 대조 검증 (forged org 방지).
    #   - 멤버십이 하나라도 있으면 JWT org 는 그중 active 하나여야 한다(아니면 403).
    #   - 멤버십이 전혀 없는 계정(백필 이전 신규 등)은 레거시(user.organization_id)로 허용.
    # 주의: role/org 를 선택 멤버십으로 "교체"하는 컨텍스트 전환은 current_user 를 mutate 해야
    # 하는데, 이는 self-update 엔드포인트(프로필/서명/PIN 등, current_user 를 직접 수정 후 commit)
    # 를 깨뜨린다. 따라서 여기서는 검증만 하고, 실제 컨텍스트 전환은 별도 CurrentContext 리팩토링
    # (컨텍스트 객체 + 전 call-site 이전)으로 미룬다. 단일 멤버십(현재/지인 트라이얼)에서는 home org
    # 컨텍스트가 곧 유일 org 라 이 검증만으로 정확하다.
    sel_org = payload.get("org")
    if sel_org is not None:
        from sqlalchemy.orm.attributes import set_committed_value
        from app.models.org_member import OrgMember

        sel_org_uuid = UUID(sel_org)
        members = (
            await db.execute(
                select(OrgMember)
                .options(selectinload(OrgMember.role))
                .where(
                    OrgMember.user_id == user.id,
                    OrgMember.status != "terminated",
                )
            )
        ).scalars().all()
        if members:
            match = next((m for m in members if m.organization_id == sel_org_uuid), None)
            if match is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not a member of the selected organization",
                )
            # 선택 org 컨텍스트를 current_user 에 반영하되, set_committed_value 로 "이미 커밋된 값"
            # 처럼 세팅한다 → 이 요청 동안 organization_id/role 이 선택 org 로 읽히지만, dirty 가
            # 아니므로 commit 시 DB 로 flush 되지 않는다(계정의 home org 는 그대로). user 는 attached
            # 상태라 self-update 엔드포인트(프로필/서명/PIN)도 정상 동작.
            #
            # ★ home org(= user.organization_id) 는 override 하지 않는다: home org 의 role 은
            # users.role_id(라이브 소스)를 그대로 쓴다. org_member 는 update_user 시 동기화되지
            # 않을 수 있어(전환기), home org 에서 org_member.role 을 신뢰하면 stale role 위험.
            # org_member.role 은 "다른 org(멀티-멤버십)" 컨텍스트에서만 권위를 갖는다.
            if match.organization_id != user.organization_id:
                set_committed_value(user, "organization_id", match.organization_id)
                set_committed_value(user, "role_id", match.role_id)
                set_committed_value(user, "role", match.role)

    # [License] 유효 org 의 라이센스가 active 가 아니거나 만료면 접근 차단 (403).
    # 운영자가 백오피스에서 라이센스 정지 → 그 org 사용자는 즉시 접근 불가.
    from app.models.license import License

    lic = (
        await db.execute(
            select(License.status, License.expires_at).where(
                License.organization_id == user.organization_id
            )
        )
    ).first()
    if lic is not None:
        lic_status, lic_expires = lic
        expired = lic_expires is not None and lic_expires < datetime.now(timezone.utc)
        if lic_status != "active" or expired:
            # 구조화된 에러 코드 — 프론트가 텍스트가 아닌 code 로 분기(전용 화면 표시).
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "ORG_LICENSE_INACTIVE",
                    "message": "Organization license is inactive",
                },
            )

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
    """사용자가 특정 매장에 접근 가능한지 확인합니다. 불가 시 403/404 발생.

    [Model B / 멀티-org IDOR 수정] 먼저 store 가 caller 의 org 소속인지 무조건 검증한다
    (Owner 도 예외 없음 — 기존엔 Owner 면 get_accessible_store_ids 가 None 이라 이 함수가
    no-op 이 되어 타 org store_id 도 통과하던 cross-tenant 결함이 있었다). 타 org 의
    store_id 는 존재를 노출하지 않도록 404 로 응답한다.
    """
    from app.models.organization import Store

    store = await db.get(Store, store_id)
    if store is None or store.organization_id != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Store not found",
        )
    # org 소속 확인 후 — 비-Owner 는 배정된 매장인지 추가 확인.
    accessible = await get_accessible_store_ids(db, user)
    if accessible is not None and store_id not in accessible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this store",
        )
