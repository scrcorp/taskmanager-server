"""스케줄 기간 레포지토리."""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import SchedulePeriod
from app.repositories.base import BaseRepository


class SchedulePeriodRepository(BaseRepository[SchedulePeriod]):

    def __init__(self) -> None:
        super().__init__(SchedulePeriod)

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[SchedulePeriod], int]:
        query: Select = select(SchedulePeriod).where(
            SchedulePeriod.organization_id == organization_id
        )
        if store_id is not None:
            query = query.where(SchedulePeriod.store_id == store_id)
        if status is not None:
            query = query.where(SchedulePeriod.status == status)
        query = query.order_by(SchedulePeriod.period_start.desc())
        return await self.get_paginated(db, query, page, per_page)

    async def check_overlap(
        self,
        db: AsyncSession,
        store_id: UUID,
        period_start: date,
        period_end: date,
        exclude_id: UUID | None = None,
    ) -> bool:
        """기간 겹침 확인."""
        query = (
            select(func.count()).select_from(SchedulePeriod)
            .where(
                SchedulePeriod.store_id == store_id,
                SchedulePeriod.period_start <= period_end,
                SchedulePeriod.period_end >= period_start,
            )
        )
        if exclude_id is not None:
            query = query.where(SchedulePeriod.id != exclude_id)
        count: int = (await db.execute(query)).scalar() or 0
        return count > 0

    async def find_overlapping(
        self,
        db: AsyncSession,
        store_id: UUID,
        period_start: date,
        period_end: date,
        exclude_id: UUID | None = None,
    ) -> "SchedulePeriod | None":
        """겹치는 기간을 반환 (첫 번째 1건)."""
        query = (
            select(SchedulePeriod)
            .where(
                SchedulePeriod.store_id == store_id,
                SchedulePeriod.period_start <= period_end,
                SchedulePeriod.period_end >= period_start,
            )
            .limit(1)
        )
        if exclude_id is not None:
            query = query.where(SchedulePeriod.id != exclude_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()


schedule_period_repository: SchedulePeriodRepository = SchedulePeriodRepository()
