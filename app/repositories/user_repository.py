"""사용자 레포지토리 — 사용자 CRUD 및 브랜드 매핑 쿼리.

User Repository — CRUD and brand mapping queries for users.
Extends BaseRepository with User-specific database operations
including filtering, eager loading, and user-brand associations.
"""

from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Brand
from app.models.user import User
from app.models.user_brand import UserBrand
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """사용자 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the users table.
    Provides organization-scoped user retrieval, filtering, and brand management.
    """

    def __init__(self) -> None:
        """UserRepository를 초기화합니다.

        Initialize the UserRepository with the User model.
        """
        super().__init__(User)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        filters: dict[str, UUID | bool | None] | None = None,
    ) -> list[User]:
        """조직에 속한 사용자 목록을 필터 조건으로 조회합니다.

        Retrieve users belonging to an organization with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            filters: 필터 딕셔너리 (brand_id, role_id, is_active)
                     (Filter dict with optional brand_id, role_id, is_active)

        Returns:
            list[User]: 사용자 목록 (List of users)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.organization_id == organization_id)
        )

        if filters:
            brand_id: UUID | None = filters.get("brand_id")  # type: ignore[assignment]
            role_id: UUID | None = filters.get("role_id")  # type: ignore[assignment]
            is_active: bool | None = filters.get("is_active")  # type: ignore[assignment]

            if brand_id is not None:
                # 특정 브랜드에 배정된 사용자만 조회
                # Only users assigned to a specific brand
                query = query.join(UserBrand, UserBrand.user_id == User.id).where(
                    UserBrand.brand_id == brand_id
                )
            if role_id is not None:
                query = query.where(User.role_id == role_id)
            if is_active is not None:
                query = query.where(User.is_active == is_active)

        query = query.order_by(User.created_at)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_detail(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> User | None:
        """사용자 상세 정보를 역할과 함께 조회합니다.

        Retrieve user detail with role eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            User | None: 역할이 로드된 사용자 또는 None
                         (User with role loaded, or None)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.id == user_id, User.organization_id == organization_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_user_brands(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[Brand]:
        """사용자에게 배정된 브랜드 목록을 조회합니다.

        Retrieve all brands assigned to a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)

        Returns:
            list[Brand]: 배정된 브랜드 목록 (List of assigned brands)
        """
        query: Select = (
            select(Brand)
            .join(UserBrand, UserBrand.brand_id == Brand.id)
            .where(UserBrand.user_id == user_id)
            .order_by(Brand.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def add_user_brand(
        self,
        db: AsyncSession,
        user_id: UUID,
        brand_id: UUID,
    ) -> UserBrand:
        """사용자에게 브랜드를 배정합니다.

        Assign a brand to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            brand_id: 브랜드 ID (Brand UUID)

        Returns:
            UserBrand: 생성된 매핑 레코드 (Created association record)
        """
        user_brand: UserBrand = UserBrand(user_id=user_id, brand_id=brand_id)
        db.add(user_brand)
        await db.flush()
        await db.refresh(user_brand)
        return user_brand

    async def remove_user_brand(
        self,
        db: AsyncSession,
        user_id: UUID,
        brand_id: UUID,
    ) -> bool:
        """사용자에게서 브랜드 배정을 해제합니다.

        Remove a brand assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            brand_id: 브랜드 ID (Brand UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the removal was successful)
        """
        query: Select = select(UserBrand).where(
            and_(UserBrand.user_id == user_id, UserBrand.brand_id == brand_id)
        )
        result = await db.execute(query)
        user_brand: UserBrand | None = result.scalar_one_or_none()

        if user_brand is None:
            return False

        await db.delete(user_brand)
        await db.flush()
        return True

    async def user_brand_exists(
        self,
        db: AsyncSession,
        user_id: UUID,
        brand_id: UUID,
    ) -> bool:
        """사용자-브랜드 매핑이 존재하는지 확인합니다.

        Check if a user-brand association already exists.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            brand_id: 브랜드 ID (Brand UUID)

        Returns:
            bool: 매핑 존재 여부 (Whether the association exists)
        """
        query: Select = select(UserBrand).where(
            and_(UserBrand.user_id == user_id, UserBrand.brand_id == brand_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none() is not None


# 싱글턴 인스턴스 — Singleton instance
user_repository: UserRepository = UserRepository()
