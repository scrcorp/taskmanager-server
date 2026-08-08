"""Access code 감사 로그 repository — 순수 DB 쿼리."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_code_audit import AccessCodeAudit


class AccessCodeAuditRepository:
    """Access code 변경 감사 기록 적재·조회."""

    async def record(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        service_key: str,
        action: str,
        actor_user_id: UUID | None = None,
        meta: dict | None = None,
    ) -> AccessCodeAudit:
        """감사 1건 적재. flush 만 하고 commit 은 호출자 트랜잭션에 맡긴다.

        `meta` 에 코드 값을 넣지 말 것 — 로그가 자격증명 사본이 된다.
        """
        row = AccessCodeAudit(
            organization_id=organization_id,
            service_key=service_key,
            actor_user_id=actor_user_id,
            action=action,
            meta=meta,
        )
        db.add(row)
        await db.flush()
        return row

    async def list_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        service_key: str | None = None,
        limit: int = 100,
    ) -> list[AccessCodeAudit]:
        """조직의 최근 코드 변경 기록 (최신순). service_key 로 좁힐 수 있음."""
        stmt = select(AccessCodeAudit).where(
            AccessCodeAudit.organization_id == organization_id
        )
        if service_key is not None:
            stmt = stmt.where(AccessCodeAudit.service_key == service_key)
        result = await db.execute(
            stmt.order_by(AccessCodeAudit.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


access_code_audit_repository = AccessCodeAuditRepository()
