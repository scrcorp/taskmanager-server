"""노동법 설정 서비스 — Labor Law Setting 비즈니스 로직.

Labor Law Setting Service — Business logic for labor law settings.
Upsert pattern: GET returns existing or defaults, PUT creates or updates.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import LaborLawSetting
from app.repositories.labor_law_repository import labor_law_repository
from app.schemas.labor_law import LaborLawSettingResponse, LaborLawSettingUpdate


class LaborLawService:

    def _to_response(self, setting: LaborLawSetting) -> LaborLawSettingResponse:
        return LaborLawSettingResponse(
            id=str(setting.id),
            store_id=str(setting.store_id),
            federal_max_weekly=setting.federal_max_weekly,
            state_max_weekly=setting.state_max_weekly,
            store_max_weekly=setting.store_max_weekly,
            overtime_threshold_daily=setting.overtime_threshold_daily,
            created_at=setting.created_at,
        )

    async def get_setting(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> LaborLawSettingResponse | None:
        setting = await labor_law_repository.get_by_store(db, store_id, organization_id)
        if setting is None:
            return None
        return self._to_response(setting)

    async def upsert_setting(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID, data: LaborLawSettingUpdate
    ) -> LaborLawSettingResponse:
        existing = await labor_law_repository.get_by_store(db, store_id, organization_id)
        if existing is not None:
            for field, value in data.model_dump().items():
                setattr(existing, field, value)
            await db.flush()
            await db.refresh(existing)
            return self._to_response(existing)
        else:
            setting = await labor_law_repository.create(db, {
                "organization_id": organization_id,
                "store_id": store_id,
                **data.model_dump(),
            })
            return self._to_response(setting)


labor_law_service: LaborLawService = LaborLawService()
