"""Clock-in PIN 감사 로그 repository — 순수 DB 쿼리."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clockin_pin_audit import ClockinPinAudit


class ClockinPinAuditRepository:
    """PIN 조회/변경 감사 기록 적재·조회."""

    async def record(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        actor_user_id: UUID,
        target_user_id: UUID,
        action: str,
        device_id: UUID | None = None,
        store_id: UUID | None = None,
        meta: dict | None = None,
    ) -> ClockinPinAudit:
        """감사 1건 적재. flush 만 하고 commit 은 호출자 트랜잭션에 맡긴다.

        `meta` 에 PIN 값을 넣지 말 것 — 로그가 자격증명 사본이 된다.
        """
        row = ClockinPinAudit(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            action=action,
            device_id=device_id,
            store_id=store_id,
            meta=meta,
        )
        db.add(row)
        await db.flush()
        return row

    async def list_for_target(
        self,
        db: AsyncSession,
        organization_id: UUID,
        target_user_id: UUID,
        limit: int = 100,
    ) -> list[ClockinPinAudit]:
        """특정 직원 PIN 에 대한 최근 기록 (최신순)."""
        result = await db.execute(
            select(ClockinPinAudit)
            .where(
                ClockinPinAudit.organization_id == organization_id,
                ClockinPinAudit.target_user_id == target_user_id,
            )
            .order_by(ClockinPinAudit.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


clockin_pin_audit_repository = ClockinPinAuditRepository()
