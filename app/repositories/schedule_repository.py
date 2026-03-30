"""스케줄 레포지토리."""

from datetime import date, timedelta
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule
from app.repositories.base import BaseRepository


class ScheduleRepository(BaseRepository[Schedule]):

    def __init__(self) -> None:
        super().__init__(Schedule)

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 100,
        sort_desc: bool = False,
        exclude_cancelled: bool = False,
    ) -> tuple[Sequence[Schedule], int]:
        query: Select = select(Schedule).where(
            Schedule.organization_id == organization_id
        )
        if store_id is not None:
            query = query.where(Schedule.store_id == store_id)
        if user_id is not None:
            query = query.where(Schedule.user_id == user_id)
        if date_from is not None:
            query = query.where(Schedule.work_date >= date_from)
        if date_to is not None:
            query = query.where(Schedule.work_date <= date_to)
        if status is not None:
            query = query.where(Schedule.status == status)
        elif exclude_cancelled:
            # status 미지정 + exclude_cancelled: cancelled/rejected 제외
            # Exclude cancelled and rejected when no specific status is requested
            query = query.where(Schedule.status.notin_(["cancelled", "rejected"]))
        if sort_desc:
            query = query.order_by(Schedule.work_date.desc(), Schedule.start_time.desc())
        else:
            query = query.order_by(Schedule.work_date, Schedule.start_time)
        return await self.get_paginated(db, query, page, per_page)

    async def check_time_overlap(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date,
        start_time_minutes: int,
        end_time_minutes: int,
        exclude_id: UUID | None = None,
    ) -> bool:
        """같은 직원+같은 날 시간 겹침 확인. 시간은 분 단위로 비교."""
        entries = await db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.work_date == work_date,
                Schedule.status != "cancelled",
            )
        )
        for entry in entries.scalars().all():
            if exclude_id and entry.id == exclude_id:
                continue
            existing_start = entry.start_time.hour * 60 + entry.start_time.minute
            existing_end = entry.end_time.hour * 60 + entry.end_time.minute
            # Handle overnight shifts
            if existing_end <= existing_start:
                existing_end += 24 * 60
            check_end = end_time_minutes
            if check_end <= start_time_minutes:
                check_end += 24 * 60
            if start_time_minutes < existing_end and check_end > existing_start:
                return True
        return False

    async def get_daily_minutes(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date,
        exclude_id: UUID | None = None,
    ) -> int:
        """해당 일 총 근무 분."""
        query = (
            select(func.coalesce(func.sum(Schedule.net_work_minutes), 0))
            .where(
                Schedule.user_id == user_id,
                Schedule.work_date == work_date,
                Schedule.status != "cancelled",
            )
        )
        if exclude_id is not None:
            query = query.where(Schedule.id != exclude_id)
        result = await db.execute(query)
        return result.scalar() or 0

    async def get_weekly_minutes(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date,
        exclude_id: UUID | None = None,
    ) -> int:
        """해당 주(일~토) 총 근무 분."""
        weekday = work_date.weekday()
        week_start = work_date - timedelta(days=(weekday + 1) % 7)
        week_end = week_start + timedelta(days=6)
        query = (
            select(func.coalesce(func.sum(Schedule.net_work_minutes), 0))
            .where(
                Schedule.user_id == user_id,
                Schedule.work_date >= week_start,
                Schedule.work_date <= week_end,
                Schedule.status != "cancelled",
            )
        )
        if exclude_id is not None:
            query = query.where(Schedule.id != exclude_id)
        result = await db.execute(query)
        return result.scalar() or 0

    async def get_by_store_date_range(
        self,
        db: AsyncSession,
        store_id: UUID,
        date_from: date,
        date_to: date,
    ) -> list[Schedule]:
        result = await db.execute(
            select(Schedule)
            .where(
                Schedule.store_id == store_id,
                Schedule.work_date >= date_from,
                Schedule.work_date <= date_to,
            )
            .order_by(Schedule.work_date, Schedule.start_time)
        )
        return list(result.scalars().all())


schedule_repository: ScheduleRepository = ScheduleRepository()
