"""Schedule Audit Log repository."""

from datetime import date as date_type, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule, ScheduleAuditLog


class ScheduleAuditLogRepository:

    async def create(
        self,
        db: AsyncSession,
        *,
        schedule_id: UUID,
        event_type: str,
        actor_id: UUID | None = None,
        actor_role: str | None = None,
        description: str | None = None,
        reason: str | None = None,
        diff: dict[str, Any] | None = None,
    ) -> ScheduleAuditLog:
        log = ScheduleAuditLog(
            schedule_id=schedule_id,
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            timestamp=datetime.now(timezone.utc),
            description=description,
            reason=reason,
            diff=diff,
        )
        db.add(log)
        await db.flush()
        return log

    async def get_by_schedule(
        self, db: AsyncSession, schedule_id: UUID,
    ) -> list[ScheduleAuditLog]:
        result = await db.execute(
            select(ScheduleAuditLog)
            .where(ScheduleAuditLog.schedule_id == schedule_id)
            .order_by(ScheduleAuditLog.timestamp.desc())
        )
        return list(result.scalars().all())

    async def delete_by_id(self, db: AsyncSession, log_id: UUID) -> bool:
        """history 항목 삭제. 없으면 False."""
        log = await db.get(ScheduleAuditLog, log_id)
        if log is None:
            return False
        await db.delete(log)
        await db.flush()
        return True

    async def list_history(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        actor_id: UUID | None = None,
        event_type: str | None = None,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[tuple[ScheduleAuditLog, Schedule]], int]:
        """집계된 history 조회. schedules와 JOIN해서 org scope 필터링.

        date_from/date_to는 schedule.work_date 기준.
        store_id/user_id는 schedule 기준.
        actor_id/event_type은 audit log 기준.
        """
        base = select(ScheduleAuditLog, Schedule).join(
            Schedule, ScheduleAuditLog.schedule_id == Schedule.id
        ).where(Schedule.organization_id == organization_id)
        if store_id is not None:
            base = base.where(Schedule.store_id == store_id)
        if user_id is not None:
            base = base.where(Schedule.user_id == user_id)
        if actor_id is not None:
            base = base.where(ScheduleAuditLog.actor_id == actor_id)
        if event_type is not None:
            base = base.where(ScheduleAuditLog.event_type == event_type)
        if date_from is not None:
            base = base.where(Schedule.work_date >= date_from)
        if date_to is not None:
            base = base.where(Schedule.work_date <= date_to)

        # Total count
        count_q = select(func.count()).select_from(base.subquery())
        total_result = await db.execute(count_q)
        total = total_result.scalar_one()

        # Page
        offset = (page - 1) * per_page
        rows = await db.execute(
            base.order_by(ScheduleAuditLog.timestamp.desc())
            .offset(offset)
            .limit(per_page)
        )
        return [(log, sched) for log, sched in rows.all()], total


schedule_audit_log_repository = ScheduleAuditLogRepository()
