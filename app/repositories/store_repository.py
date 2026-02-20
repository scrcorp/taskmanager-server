"""매장 레포지토리 — 매장 CRUD 및 관련 쿼리.

Store Repository — CRUD and related queries for stores.
Extends BaseRepository with Store-specific database operations
including shift/position eager loading.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Store
from app.repositories.base import BaseRepository


class StoreRepository(BaseRepository[Store]):
    """매장 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the stores table.
    Provides organization-scoped store retrieval and detail loading.
    """

    def __init__(self) -> None:
        """StoreRepository를 초기화합니다.

        Initialize the StoreRepository with the Store model.
        """
        super().__init__(Store)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[Store]:
        """조직에 속한 모든 매장을 조회합니다.

        Retrieve all stores belonging to a specific organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[Store]: 매장 목록 (List of stores)
        """
        query: Select = (
            select(Store)
            .where(Store.organization_id == organization_id)
            .order_by(Store.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_detail(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> Store | None:
        """매장 상세 정보를 근무조/직책과 함께 조회합니다.

        Retrieve store detail with shifts and positions eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            Store | None: 근무조/직책이 로드된 매장 또는 None
                          (Store with shifts/positions loaded, or None)
        """
        query: Select = (
            select(Store)
            .options(selectinload(Store.shifts), selectinload(Store.positions))
            .where(Store.id == store_id, Store.organization_id == organization_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


# 싱글턴 인스턴스 — Singleton instance
store_repository: StoreRepository = StoreRepository()
