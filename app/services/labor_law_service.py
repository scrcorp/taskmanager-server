"""노동법 설정 서비스 — Labor Law Setting 비즈니스 로직.

Labor Law Setting Service — Business logic for labor law settings.
Upsert pattern: GET returns existing or defaults, PUT creates or updates.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import LaborLawSetting
from app.repositories.labor_law_repository import labor_law_repository
from app.schemas.labor_law import LaborLawSettingResponse, LaborLawSettingUpdate


# 주간 최대 근무시간 기본값 (시간 단위) — LaborLawSetting row/값이 없을 때 fallback.
DEFAULT_MAX_WEEKLY_HOURS = 40


async def resolve_weekly_max_hours(db: AsyncSession, store_id: UUID) -> int:
    """해당 매장의 주간 최대 근무시간(시간 단위)을 결정한다.

    Cascade: store_max_weekly > state_max_weekly > federal_max_weekly.
    해당 매장의 LaborLawSetting row 가 없거나 값이 모두 None 이면 기본 40.

    반드시 store_id 로 필터한다 — org 내 임의 매장의 설정이 다른 매장에
    적용되는 버그(BLL-2) 방지. 초과근무 판정 등 attendance 도메인 공용.
    """
    result = await db.execute(
        select(LaborLawSetting)
        .where(LaborLawSetting.store_id == store_id)
        .limit(1)
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        return DEFAULT_MAX_WEEKLY_HOURS
    return (
        setting.store_max_weekly
        or setting.state_max_weekly
        or setting.federal_max_weekly
        or DEFAULT_MAX_WEEKLY_HOURS
    )


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
        try:
            existing = await labor_law_repository.get_by_store(db, store_id, organization_id)
            if existing is not None:
                for field, value in data.model_dump().items():
                    setattr(existing, field, value)
                await db.flush()
                await db.refresh(existing)
                result = self._to_response(existing)
            else:
                setting = await labor_law_repository.create(db, {
                    "organization_id": organization_id,
                    "store_id": store_id,
                    **data.model_dump(),
                })
                result = self._to_response(setting)

            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


labor_law_service: LaborLawService = LaborLawService()
