"""프로필 서비스 — 현재 사용자 프로필 조회/수정 비즈니스 로직.

Profile Service — Business logic for current user's profile read/update.
Handles self-service profile management via the app-facing API.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Role, User
from app.schemas.user import ProfileResponse, ProfileUpdate


class ProfileService:
    """프로필 관련 비즈니스 로직을 처리하는 서비스.

    Service handling profile business logic.
    Provides read and update operations for the current user's own profile.
    """

    async def _resolve_role_name(self, db: AsyncSession, role_id: UUID) -> str:
        """역할 ID로 역할 이름을 조회합니다.

        Resolve role name from a role ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            role_id: 역할 UUID (Role UUID)

        Returns:
            str: 역할 이름 또는 "Unknown" (Role name or "Unknown")
        """
        result = await db.execute(
            select(Role.name).where(Role.id == role_id)
        )
        role_name: str | None = result.scalar()
        return role_name or "Unknown"

    def _to_response(self, user: User, role_name: str) -> ProfileResponse:
        """사용자 모델을 프로필 응답 스키마로 변환합니다.

        Convert a User model instance to a ProfileResponse schema.

        Args:
            user: 사용자 모델 인스턴스 (User model instance)
            role_name: 조회된 역할 이름 (Resolved role name)

        Returns:
            ProfileResponse: 프로필 응답 (Profile response)
        """
        return ProfileResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            role_name=role_name,
            organization_id=str(user.organization_id),
        )

    async def get_profile(
        self,
        db: AsyncSession,
        current_user: User,
    ) -> ProfileResponse:
        """현재 사용자의 프로필을 조회합니다.

        Retrieve the current user's profile.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            current_user: 인증된 사용자 모델 (Authenticated user model)

        Returns:
            ProfileResponse: 프로필 응답 (Profile response)
        """
        role_name: str = await self._resolve_role_name(db, current_user.role_id)
        return self._to_response(current_user, role_name)

    async def update_profile(
        self,
        db: AsyncSession,
        current_user: User,
        data: ProfileUpdate,
    ) -> ProfileResponse:
        """현재 사용자의 프로필을 업데이트합니다.

        Update the current user's profile with provided fields.
        Only non-None fields from the update data are applied.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            current_user: 인증된 사용자 모델 (Authenticated user model)
            data: 업데이트 데이터 (Profile update data)

        Returns:
            ProfileResponse: 업데이트된 프로필 응답 (Updated profile response)
        """
        # None이 아닌 필드만 업데이트 — Only update non-None (provided) fields
        update_data: dict = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if hasattr(current_user, field):
                setattr(current_user, field, value)

        await db.flush()
        await db.refresh(current_user)

        role_name: str = await self._resolve_role_name(db, current_user.role_id)
        return self._to_response(current_user, role_name)


# 싱글턴 인스턴스 — Singleton instance
profile_service: ProfileService = ProfileService()
