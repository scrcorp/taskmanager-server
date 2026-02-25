"""FastAPI 의존성 주입 모듈 — 인증 및 권한 검사.

FastAPI dependency injection module — Authentication and authorization.
Provides reusable dependencies for extracting the current user from JWT
and enforcing permission-based access control on API endpoints.
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
from app.repositories.permission_repository import permission_repository

security: HTTPBearer = HTTPBearer()


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
        user_perms = await get_user_permissions(db, current_user.role_id)
        for code in permission_codes:
            if code not in user_perms:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: {code}",
                )
        return current_user
    return _check


async def get_accessible_store_ids(
    db: AsyncSession, user: User
) -> list[UUID] | None:
    """사용자가 접근 가능한 매장 ID 목록을 반환합니다.

    None means full access (priority <= 10). Empty list means no stores assigned.
    """
    if user.role.priority <= 10:
        return None
    from app.repositories.user_repository import user_repository
    return await user_repository.get_user_store_ids(db, user.id)


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
