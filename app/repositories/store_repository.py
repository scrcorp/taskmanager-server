"""매장 레포지토리 — 매장 CRUD 및 관련 쿼리.

Store Repository — CRUD and related queries for stores.
Extends BaseRepository with Store-specific database operations
including shift/position eager loading.
"""

from uuid import UUID

from sqlalchemy import Select, func, select
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
        include_closed: bool = False,
    ) -> list[Store]:
        """조직에 속한 모든 매장을 조회합니다.

        Retrieve all stores belonging to a specific organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            include_closed: closed(폐점/soft-delete) 매장 포함 여부 (복구 화면용)

        Returns:
            list[Store]: 매장 목록 (List of stores)
        """
        query: Select = select(Store).where(Store.organization_id == organization_id)
        if not include_closed:
            query = query.where(Store.deleted_at.is_(None))  # 폐점 제외
        query = query.order_by(Store.sort_order, Store.created_at)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def code_exists(
        self,
        db: AsyncSession,
        organization_id: UUID,
        code: str,
        exclude_id: UUID | None = None,
    ) -> bool:
        """org 내에 동일 code 를 가진 살아있는 매장이 있는지 확인.

        Check whether a live store (deleted_at IS NULL) in the org already uses
        `code`. Mirrors the partial-unique index uq_store_org_code (closed/soft-
        deleted stores release their code). exclude_id skips the store itself (edit).
        """
        query = select(func.count()).select_from(Store).where(
            Store.organization_id == organization_id,
            Store.code == code,
            Store.deleted_at.is_(None),
        )
        if exclude_id is not None:
            query = query.where(Store.id != exclude_id)
        count = (await db.execute(query)).scalar() or 0
        return count > 0

    async def get_max_sort_order(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> int:
        """조직 내 매장 sort_order 최대값을 반환 (없으면 -1, +1 하면 0부터 시작).

        Return the max sort_order among an org's live stores (-1 if none).
        """
        query = select(func.max(Store.sort_order)).where(
            Store.organization_id == organization_id,
            Store.deleted_at.is_(None),
        )
        result = await db.execute(query)
        max_val = result.scalar()
        return max_val if max_val is not None else -1

    async def reorder(
        self,
        db: AsyncSession,
        organization_id: UUID,
        ordered_ids: list[UUID],
    ) -> int:
        """매장 정렬 순서를 일괄 갱신합니다. ordered_ids 순서대로 sort_order 0..N 부여.

        Bulk-update store sort_order to match the given id order. org-scoped.
        Returns the number of stores updated.
        """
        updated = 0
        for idx, store_id in enumerate(ordered_ids):
            store = await self.get_by_id(db, store_id, organization_id)
            if store is None:
                continue
            store.sort_order = idx
            updated += 1
        await db.flush()
        return updated

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
