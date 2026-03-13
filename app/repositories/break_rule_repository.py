"""휴게 규칙 레포지토리."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import StoreBreakRule
from app.repositories.base import BaseRepository


class BreakRuleRepository(BaseRepository[StoreBreakRule]):
    def __init__(self) -> None:
        super().__init__(StoreBreakRule)

    async def get_by_store(
        self, db: AsyncSession, store_id: UUID
    ) -> StoreBreakRule | None:
        result = await db.execute(
            select(StoreBreakRule).where(StoreBreakRule.store_id == store_id)
        )
        return result.scalar_one_or_none()


break_rule_repository: BreakRuleRepository = BreakRuleRepository()
