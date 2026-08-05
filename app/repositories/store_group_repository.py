"""매장 그룹 레포지토리 — store_groups CRUD 및 관련 쿼리.

Store Group Repository — CRUD and related queries for store groups.
Extends BaseRepository with org-scoped retrieval and reorder.
"""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store, StoreGroup
from app.repositories.base import BaseRepository


class StoreGroupRepository(BaseRepository[StoreGroup]):
    """store_groups 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the store_groups table.
    """

    def __init__(self) -> None:
        """StoreGroupRepository를 초기화합니다."""
        super().__init__(StoreGroup)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[StoreGroup]:
        """조직에 속한 모든 그룹을 정렬 순서대로 조회합니다.

        Retrieve all store groups of an organization, ordered for display.
        """
        query: Select = (
            select(StoreGroup)
            .where(StoreGroup.organization_id == organization_id)
            .order_by(StoreGroup.sort_order, StoreGroup.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def store_counts(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> dict[UUID, int]:
        """그룹별 소속 매장 수 — {group_id: count}. 폐점(soft-delete) 매장 제외.

        Per-group live store counts (closed/soft-deleted stores excluded).
        """
        rows = (
            await db.execute(
                select(Store.group_id, func.count().label("cnt"))
                .where(
                    Store.organization_id == organization_id,
                    Store.group_id.isnot(None),
                    Store.deleted_at.is_(None),
                )
                .group_by(Store.group_id)
            )
        ).all()
        return {r.group_id: r.cnt for r in rows}

    async def get_max_sort_order(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> int:
        """조직 내 그룹 sort_order 최대값을 반환 (없으면 -1, +1 하면 0부터 시작)."""
        query = select(func.max(StoreGroup.sort_order)).where(
            StoreGroup.organization_id == organization_id,
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
        """그룹 정렬 순서를 일괄 갱신합니다. ordered_ids 순서대로 sort_order 0..N 부여.

        Bulk-update group sort_order to match the given id order. org-scoped.
        """
        updated = 0
        for idx, group_id in enumerate(ordered_ids):
            group = await self.get_by_id(db, group_id, organization_id)
            if group is None:
                continue
            group.sort_order = idx
            updated += 1
        await db.flush()
        return updated


# 싱글턴 인스턴스 — Singleton instance
store_group_repository: StoreGroupRepository = StoreGroupRepository()
