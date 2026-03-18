"""업무 역할 레포지토리."""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import StoreWorkRole
from app.repositories.base import BaseRepository


class WorkRoleRepository(BaseRepository[StoreWorkRole]):
    def __init__(self) -> None:
        super().__init__(StoreWorkRole)

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        active_only: bool = False,
    ) -> list[StoreWorkRole]:
        query: Select = select(StoreWorkRole).where(
            StoreWorkRole.store_id == store_id
        )
        if active_only:
            query = query.where(StoreWorkRole.is_active.is_(True))
        query = query.order_by(StoreWorkRole.sort_order)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def check_duplicate(
        self,
        db: AsyncSession,
        store_id: UUID,
        shift_id: UUID,
        position_id: UUID,
        exclude_id: UUID | None = None,
    ) -> bool:
        query = (
            select(func.count())
            .select_from(StoreWorkRole)
            .where(
                StoreWorkRole.store_id == store_id,
                StoreWorkRole.shift_id == shift_id,
                StoreWorkRole.position_id == position_id,
            )
        )
        if exclude_id is not None:
            query = query.where(StoreWorkRole.id != exclude_id)
        count: int = (await db.execute(query)).scalar() or 0
        return count > 0


work_role_repository: WorkRoleRepository = WorkRoleRepository()
