"""시프트 프리셋 레포지토리 — Shift Preset CRUD.

Shift Preset Repository — CRUD queries for shift_presets table.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import ShiftPreset
from app.repositories.base import BaseRepository


class ShiftPresetRepository(BaseRepository[ShiftPreset]):

    def __init__(self) -> None:
        super().__init__(ShiftPreset)

    async def get_by_store(
        self, db: AsyncSession, store_id: UUID
    ) -> list[ShiftPreset]:
        query: Select = (
            select(ShiftPreset)
            .where(ShiftPreset.store_id == store_id)
            .order_by(ShiftPreset.sort_order)
        )
        result = await db.execute(query)
        return list(result.scalars().all())


shift_preset_repository: ShiftPresetRepository = ShiftPresetRepository()
