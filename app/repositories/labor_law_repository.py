"""노동법 설정 레포지토리 — Labor Law Setting CRUD.

Labor Law Setting Repository — CRUD queries for labor_law_settings table.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import LaborLawSetting
from app.repositories.base import BaseRepository


class LaborLawRepository(BaseRepository[LaborLawSetting]):

    def __init__(self) -> None:
        super().__init__(LaborLawSetting)

    async def get_by_store(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> LaborLawSetting | None:
        query: Select = (
            select(LaborLawSetting)
            .where(
                LaborLawSetting.store_id == store_id,
                LaborLawSetting.organization_id == organization_id,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


labor_law_repository: LaborLawRepository = LaborLawRepository()
