"""인증 레포지토리 — 리프레시 토큰 CRUD 및 사용자 조회.

Auth Repository — Handles refresh token CRUD and user lookup by username.
Provides database operations for authentication workflows including
token lifecycle management and credential-based user retrieval.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.token import RefreshToken
from app.models.user import Role, User


class AuthRepository:
    """인증 관련 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling authentication-related database queries.
    Manages refresh token lifecycle and user credential lookups.
    """

    async def get_user_by_username(
        self,
        db: AsyncSession,
        username: str,
        organization_id: UUID | None = None,
    ) -> User | None:
        """사용자명으로 사용자를 조회합니다.

        Retrieve a user by username, optionally scoped to an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            username: 조회할 사용자명 (Username to look up)
            organization_id: 조직 범위 필터, None이면 전체 검색
                             (Organization scope filter; None searches all)

        Returns:
            User | None: 조회된 사용자 또는 None (Found user or None)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.username == username)
        )
        if organization_id is not None:
            query = query.where(User.organization_id == organization_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def create_refresh_token(
        self,
        db: AsyncSession,
        user_id: UUID,
        token: str,
        expires_at: datetime,
    ) -> RefreshToken:
        """새 리프레시 토큰을 생성합니다.

        Create a new refresh token record in the database.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 토큰 소유자 사용자 ID (Token owner user UUID)
            token: JWT 리프레시 토큰 문자열 (JWT refresh token string)
            expires_at: 토큰 만료 일시 (Token expiration timestamp)

        Returns:
            RefreshToken: 생성된 리프레시 토큰 레코드 (Created refresh token record)
        """
        db_token: RefreshToken = RefreshToken(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
        )
        db.add(db_token)
        await db.flush()
        await db.refresh(db_token)
        return db_token

    async def get_refresh_token(
        self,
        db: AsyncSession,
        token: str,
    ) -> RefreshToken | None:
        """리프레시 토큰 문자열로 토큰 레코드를 조회합니다.

        Retrieve a refresh token record by its token string.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            token: 조회할 JWT 리프레시 토큰 문자열 (JWT refresh token string to look up)

        Returns:
            RefreshToken | None: 조회된 토큰 레코드 또는 None (Found token record or None)
        """
        query: Select = select(RefreshToken).where(RefreshToken.token == token)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def delete_refresh_token(
        self,
        db: AsyncSession,
        token: str,
    ) -> bool:
        """리프레시 토큰을 삭제합니다.

        Delete a specific refresh token by its token string.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            token: 삭제할 리프레시 토큰 문자열 (Refresh token string to delete)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)
        """
        db_token: RefreshToken | None = await self.get_refresh_token(db, token)
        if db_token is None:
            return False

        await db.delete(db_token)
        await db.flush()
        return True

    async def delete_user_refresh_tokens(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> None:
        """특정 사용자의 모든 리프레시 토큰을 삭제합니다.

        Delete all refresh tokens for a specific user (logout from all devices).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 대상 사용자 ID (Target user UUID)
        """
        stmt = delete(RefreshToken).where(RefreshToken.user_id == user_id)
        await db.execute(stmt)
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
auth_repository: AuthRepository = AuthRepository()
