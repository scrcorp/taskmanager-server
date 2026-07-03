"""org 번호(crewid) / 매장 번호(empid) 다음 순번 계산.

crewid = org 안에서 1부터, empid = 매장(store) 안에서 1부터. 부여 규칙 없이 단순 MAX+1.
동시성: MAX+1 은 경합 시 같은 번호가 날 수 있으나 partial unique 로 걸린다(트라이얼 단계 수용).
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember, OrgMemberStore


async def next_crewid(db: AsyncSession, organization_id: UUID) -> int:
    """org 안에서 다음 crewid (1부터)."""
    return (
        await db.execute(
            select(func.coalesce(func.max(OrgMember.crewid), 0) + 1).where(
                OrgMember.organization_id == organization_id
            )
        )
    ).scalar() or 1


async def next_empid(db: AsyncSession, store_id: UUID) -> int:
    """매장 안에서 다음 empid (1부터)."""
    return (
        await db.execute(
            select(func.coalesce(func.max(OrgMemberStore.empid), 0) + 1).where(
                OrgMemberStore.store_id == store_id
            )
        )
    ).scalar() or 1
