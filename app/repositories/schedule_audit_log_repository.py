"""Schedule Audit Log repository."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import ScheduleAuditLog


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


schedule_audit_log_repository = ScheduleAuditLogRepository()
