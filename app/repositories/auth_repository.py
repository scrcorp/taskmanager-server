"""인증 레포지토리 — 리프레시 토큰 CRUD 및 사용자 조회.

Auth Repository — Handles refresh token CRUD and user lookup by username.
Provides database operations for authentication workflows including
token lifecycle management and credential-based user retrieval.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, delete, func, select
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
        client_type: str = "unknown",
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        """새 리프레시 토큰을 생성합니다.

        Create a new refresh token record in the database.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 토큰 소유자 사용자 ID (Token owner user UUID)
            token: JWT 리프레시 토큰 문자열 (JWT refresh token string)
            expires_at: 토큰 만료 일시 (Token expiration timestamp)
            client_type: 클라이언트 유형 "admin" | "app" (Client type)
            user_agent: User-Agent 원본 (Raw User-Agent string)
            ip_address: 접속 IP (Client IP address)

        Returns:
            RefreshToken: 생성된 리프레시 토큰 레코드 (Created refresh token record)
        """
        db_token: RefreshToken = RefreshToken(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
            client_type=client_type,
            user_agent=user_agent,
            ip_address=ip_address,
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

    async def count_user_sessions(
        self,
        db: AsyncSession,
        user_id: UUID,
        client_type: str,
    ) -> int:
        """특정 사용자의 client_type별 세션 수를 조회합니다.

        Count the number of active sessions for a user by client_type.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            client_type: 클라이언트 유형 (Client type: "admin" | "app")

        Returns:
            int: 세션 수 (Number of active sessions)
        """
        result = await db.execute(
            select(func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.client_type == client_type,
            )
        )
        return result.scalar() or 0

    async def delete_oldest_sessions(
        self,
        db: AsyncSession,
        user_id: UUID,
        client_type: str,
        keep_count: int,
    ) -> None:
        """가장 오래된 세션을 삭제하여 keep_count개만 남깁니다.

        Delete oldest sessions to keep only keep_count sessions.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            client_type: 클라이언트 유형 (Client type)
            keep_count: 유지할 세션 수 (Number of sessions to keep)
        """
        # Get IDs to keep (most recently used)
        keep_subq = (
            select(RefreshToken.id)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.client_type == client_type,
            )
            .order_by(RefreshToken.last_used_at.desc())
            .limit(keep_count)
        )
        # Delete the rest
        stmt = delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.client_type == client_type,
            RefreshToken.id.notin_(keep_subq),
        )
        await db.execute(stmt)
        await db.flush()

    async def update_session_activity(
        self,
        db: AsyncSession,
        token_id: UUID,
        ip_address: str | None = None,
    ) -> None:
        """세션의 last_used_at과 ip_address를 갱신합니다.

        Update session activity timestamp and IP address.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            token_id: 토큰 레코드 ID (Token record UUID)
            ip_address: 접속 IP (Client IP address)
        """
        from datetime import timezone as tz

        result = await db.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        db_token = result.scalar_one_or_none()
        if db_token:
            db_token.last_used_at = datetime.now(tz.utc)
            if ip_address:
                db_token.ip_address = ip_address
            await db.flush()

    async def delete_expired_tokens(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> None:
        """만료된 리프레시 토큰을 정리합니다.

        Clean up expired refresh tokens for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
        """
        from datetime import timezone as tz

        stmt = delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.expires_at < datetime.now(tz.utc),
        )
        await db.execute(stmt)
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
auth_repository: AuthRepository = AuthRepository()
