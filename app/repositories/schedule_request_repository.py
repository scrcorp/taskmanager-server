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
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[Sequence[ScheduleRequest], int]:
        query: Select = select(ScheduleRequest)
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

    async def get_by_store_date_range_user(
        self,
        db: AsyncSession,
        store_id: UUID,
        user_id: UUID,
        date_from: date_type,
        date_to: date_type,
    ) -> list[ScheduleRequest]:
        result = await db.execute(
            select(ScheduleRequest)
            .where(
                ScheduleRequest.store_id == store_id,
                ScheduleRequest.user_id == user_id,
                ScheduleRequest.work_date >= date_from,
                ScheduleRequest.work_date <= date_to,
            )
            .order_by(ScheduleRequest.work_date)
        )
        return list(result.scalars().all())

    async def find_active_duplicate(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date_type,
        work_role_id: UUID | None,
    ) -> ScheduleRequest | None:
        """같은 user + date + work_role 조합의 non-rejected request 조회."""
        query = select(ScheduleRequest).where(
            ScheduleRequest.user_id == user_id,
            ScheduleRequest.work_date == work_date,
            ScheduleRequest.status != "rejected",
        )
        if work_role_id is not None:
            query = query.where(ScheduleRequest.work_role_id == work_role_id)
        else:
            query = query.where(ScheduleRequest.work_role_id.is_(None))
        result = await db.execute(query.limit(1))
        return result.scalar_one_or_none()


schedule_request_repository: ScheduleRequestRepository = ScheduleRequestRepository()
