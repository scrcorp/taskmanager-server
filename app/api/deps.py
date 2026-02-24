"""FastAPI 의존성 주입 모듈 — 인증 및 권한 검사.

FastAPI dependency injection module — Authentication and authorization.
Provides reusable dependencies for extracting the current user from JWT
and enforcing role-based access control (RBAC) on API endpoints.

Authentication Flow:
    1. 클라이언트가 Authorization: Bearer <token> 헤더를 전송
       (Client sends Authorization: Bearer <token> header)
    2. HTTPBearer가 토큰을 추출 (HTTPBearer extracts the token)
    3. decode_token()이 JWT를 검증하고 페이로드를 반환
       (decode_token verifies JWT and returns payload)
    4. 페이로드의 "sub" 필드로 DB에서 사용자를 조회
       (User is fetched from DB using payload "sub" field)
    5. 사용자 활성 상태를 확인 (User active status is verified)

Authorization Flow (require_level):
    1. get_current_user로 사용자 인증 (User authenticated via get_current_user)
    2. 사용자의 역할 레벨을 DB에서 조회 (Role level fetched from DB)
    3. 역할 레벨이 max_level 이하인지 확인 (Level checked against max_level)
    4. 레벨이 높으면(숫자가 크면) 403 Forbidden 반환
       (Returns 403 if level exceeds max_level)
"""

from typing import Annotated, Callable, Awaitable
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.utils.jwt import decode_token
from app.models.user import User

# HTTP Bearer 토큰 추출기 — Authorization 헤더에서 JWT 토큰 추출
# (Extracts JWT token from Authorization: Bearer <token> header)
security: HTTPBearer = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """JWT 토큰에서 현재 인증된 사용자를 추출합니다.

    Decode JWT from the Authorization header and return the authenticated user.
    Validates token signature, expiration, and user existence/active status.

    Args:
        credentials: HTTP Bearer 토큰 자격 증명 (Bearer token credentials from header)
        db: 비동기 DB 세션 (Async database session)

    Returns:
        User: 인증된 사용자 ORM 인스턴스 (Authenticated user ORM instance)

    Raises:
        HTTPException(401): 토큰이 유효하지 않거나 만료됨 (Invalid or expired token)
        HTTPException(401): 사용자를 찾을 수 없거나 비활성 (User not found or inactive)
    """
    try:
        payload: dict = decode_token(credentials.credentials)
        # 토큰 타입 검증 — Reject refresh tokens used as access tokens
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

    return user


def require_level(max_level: int) -> Callable[..., Awaitable[User]]:
    """역할 레벨 기반 권한 검사 의존성 팩토리.

    Dependency factory that creates a FastAPI dependency enforcing
    a maximum role level. Lower level = higher authority.

    Level hierarchy:
        1 = owner (최고 권한, highest authority)
        2 = general_manager
        3 = supervisor
        4 = staff (최저 권한, lowest authority)

    Args:
        max_level: 허용되는 최대 역할 레벨 (Maximum allowed role level, inclusive)

    Returns:
        FastAPI 의존성 함수 — 인증된 사용자 반환 또는 403 발생
        (FastAPI dependency function that returns User or raises 403)
    """
    async def _check(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        # role은 get_current_user에서 selectinload로 이미 로드됨
        # Role is already eager-loaded via selectinload in get_current_user
        role = current_user.role
        if role is None or role.level > max_level:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return _check


# 편의 의존성 — Pre-configured level dependencies for common role requirements
require_owner = require_level(1)       # Owner만 허용 (Owner only, level 1)
require_gm = require_level(2)          # Owner + GM 허용 (Owner + General Manager, level <= 2)
require_supervisor = require_level(3)  # Owner + GM + Supervisor 허용 (Level <= 3)


async def get_accessible_store_ids(
    db: AsyncSession, user: User
) -> list[UUID] | None:
    """사용자가 접근 가능한 매장 ID 목록을 반환합니다.

    Return the list of store IDs accessible to the user.
    None means full access (Owner). Empty list means no stores assigned.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        user: 현재 사용자 (Current user with role loaded)

    Returns:
        list[UUID] | None: 매장 ID 목록 또는 None (전체 접근)
                           (Store ID list, or None for full access)
    """
    if user.role.level <= 1:
        return None
    from app.repositories.user_repository import user_repository
    return await user_repository.get_user_store_ids(db, user.id)


async def check_store_access(
    db: AsyncSession, user: User, store_id: UUID
) -> None:
    """사용자가 특정 매장에 접근 가능한지 확인합니다. 불가 시 403 발생.

    Verify the user has access to a specific store. Raises 403 if not.
    Owner has full access. GM/Supervisor must have the store in their user_stores.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        user: 현재 사용자 (Current user with role loaded)
        store_id: 확인할 매장 ID (Store UUID to check access for)

    Raises:
        HTTPException(403): 매장 접근 권한 없음 (No access to this store)
    """
    accessible = await get_accessible_store_ids(db, user)
    if accessible is not None and store_id not in accessible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this store",
        )
