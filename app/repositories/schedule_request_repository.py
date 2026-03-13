"""스케줄 신청 레포지토리."""

from datetime import date as date_type
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import ScheduleRequest
from app.repositories.base import BaseRepository


class ScheduleRequestRepository(BaseRepository[ScheduleRequest]):

    def __init__(self) -> None:
        super().__init__(ScheduleRequest)

    async def get_by_filters(
        self,
        db: AsyncSession,
        period_id: UUID | None = None,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[Sequence[ScheduleRequest], int]:
        query: Select = select(ScheduleRequest)
        if period_id is not None:
            query = query.where(ScheduleRequest.period_id == period_id)
        if store_id is not None:
            query = query.where(ScheduleRequest.store_id == store_id)
        if user_id is not None:
            query = query.where(ScheduleRequest.user_id == user_id)
        if date_from is not None:
            query = query.where(ScheduleRequest.work_date >= date_from)
        if date_to is not None:
            query = query.where(ScheduleRequest.work_date <= date_to)
        query = query.order_by(ScheduleRequest.work_date, ScheduleRequest.created_at)
        return await self.get_paginated(db, query, page, per_page)

    async def get_by_period_user(
        self, db: AsyncSession, period_id: UUID, user_id: UUID,
    ) -> list[ScheduleRequest]:
        result = await db.execute(
            select(ScheduleRequest)
            .where(ScheduleRequest.period_id == period_id, ScheduleRequest.user_id == user_id)
            .order_by(ScheduleRequest.work_date)
        )
        return list(result.scalars().all())

    async def get_by_previous_period_user(
        self, db: AsyncSession, previous_period_id: UUID, user_id: UUID,
    ) -> list[ScheduleRequest]:
        """이전 기간의 사용자 신청 목록 조회 (copy용)."""
        result = await db.execute(
            select(ScheduleRequest)
            .where(
                ScheduleRequest.period_id == previous_period_id,
                ScheduleRequest.user_id == user_id,
            )
            .order_by(ScheduleRequest.work_date)
        )
        return list(result.scalars().all())


schedule_request_repository: ScheduleRequestRepository = ScheduleRequestRepository()
