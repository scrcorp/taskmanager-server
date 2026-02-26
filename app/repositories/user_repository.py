"""사용자 레포지토리 — 사용자 CRUD 및 매장 매핑 쿼리.

User Repository — CRUD and store mapping queries for users.
Extends BaseRepository with User-specific database operations
including filtering, eager loading, and user-store associations.
"""

from uuid import UUID

from sqlalchemy import Select, and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Store
from app.models.user import User
from app.models.user_store import UserStore
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """사용자 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the users table.
    Provides organization-scoped user retrieval, filtering, and store management.
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
            filters: 필터 딕셔너리 (store_id, role_id, is_active)
                     (Filter dict with optional store_id, role_id, is_active)

        Returns:
            list[User]: 사용자 목록 (List of users)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.organization_id == organization_id)
        )

        if filters:
            store_id: UUID | None = filters.get("store_id")  # type: ignore[assignment]
            role_id: UUID | None = filters.get("role_id")  # type: ignore[assignment]
            is_active: bool | None = filters.get("is_active")  # type: ignore[assignment]

            if store_id is not None:
                # 특정 매장에 배정된 사용자만 조회
                # Only users assigned to a specific store
                query = query.join(UserStore, UserStore.user_id == User.id).where(
                    UserStore.store_id == store_id
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

    async def get_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[Store]:
        """사용자에게 배정된 매장 목록을 조회합니다.

        Retrieve all stores assigned to a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)

        Returns:
            list[Store]: 배정된 매장 목록 (List of assigned stores)
        """
        query: Select = (
            select(Store)
            .join(UserStore, UserStore.store_id == Store.id)
            .where(UserStore.user_id == user_id)
            .order_by(Store.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def add_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> UserStore:
        """사용자에게 매장을 배정합니다.

        Assign a store to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            UserStore: 생성된 매핑 레코드 (Created association record)
        """
        user_store: UserStore = UserStore(user_id=user_id, store_id=store_id)
        db.add(user_store)
        await db.flush()
        await db.refresh(user_store)
        return user_store

    async def remove_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> bool:
        """사용자에게서 매장 배정을 해제합니다.

        Remove a store assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the removal was successful)
        """
        query: Select = select(UserStore).where(
            and_(UserStore.user_id == user_id, UserStore.store_id == store_id)
        )
        result = await db.execute(query)
        user_store: UserStore | None = result.scalar_one_or_none()

        if user_store is None:
            return False

        await db.delete(user_store)
        await db.flush()
        return True

    async def get_user_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """사용자에게 배정된 매장 ID 목록을 반환합니다.

        Return the list of store IDs assigned to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)

        Returns:
            list[UUID]: 배정된 매장 ID 목록 (List of assigned store IDs)
        """
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def get_managed_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """관리매장 ID 목록 (is_manager=true)."""
        result = await db.execute(
            select(UserStore.store_id).where(
                UserStore.user_id == user_id, UserStore.is_manager.is_(True)
            )
        )
        return list(result.scalars().all())

    async def get_work_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """근무매장 ID 목록 (모든 user_stores)."""
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def get_user_store_assignments(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UserStore]:
        """사용자의 전체 매장 배정 레코드 조회."""
        result = await db.execute(
            select(UserStore).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def sync_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        assignments: list[dict],
    ) -> None:
        """일괄 저장: 현재 상태와 diff 계산 후 추가/수정/삭제.

        Args:
            assignments: [{"store_id": UUID, "is_manager": bool}, ...]
        """
        # 현재 상태 조회
        current = await self.get_user_store_assignments(db, user_id)
        current_map: dict[UUID, UserStore] = {us.store_id: us for us in current}

        # 목표 상태
        target_map: dict[UUID, bool] = {
            a["store_id"]: a["is_manager"] for a in assignments
        }

        # 삭제: 현재에 있지만 목표에 없는 것
        to_delete = set(current_map.keys()) - set(target_map.keys())
        if to_delete:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id.in_(to_delete),
                )
            )

        # 추가/수정
        for store_id, is_manager in target_map.items():
            existing = current_map.get(store_id)
            if existing is None:
                db.add(UserStore(
                    user_id=user_id, store_id=store_id, is_manager=is_manager
                ))
            elif existing.is_manager != is_manager:
                existing.is_manager = is_manager

        await db.flush()

    async def user_store_exists(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> bool:
        """사용자-매장 매핑이 존재하는지 확인합니다.

        Check if a user-store association already exists.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            bool: 매핑 존재 여부 (Whether the association exists)
        """
        query: Select = select(UserStore).where(
            and_(UserStore.user_id == user_id, UserStore.store_id == store_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none() is not None


# 싱글턴 인스턴스 — Singleton instance
user_repository: UserRepository = UserRepository()
