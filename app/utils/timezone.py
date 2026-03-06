"""타임존 유틸리티 — 매장/조직 타임존 해석 헬퍼.

Timezone utility — helpers for resolving store/organization timezone.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store

DEFAULT_TIMEZONE = "America/Los_Angeles"


async def get_store_timezone(db: AsyncSession, store_id: UUID) -> str:
    """매장의 유효 타임존을 반환합니다 (매장 → 조직 → 기본값 순).

    Resolve effective timezone for a store (store → organization → default).

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        store_id: 매장 UUID (Store UUID)

    Returns:
        str: IANA 타임존 문자열 (IANA timezone string)
    """
    result = await db.execute(
        select(Store.timezone, Organization.timezone.label("org_timezone"))
        .join(Organization, Store.organization_id == Organization.id)
        .where(Store.id == store_id)
    )
    row = result.one_or_none()
    if row is None:
        return DEFAULT_TIMEZONE
    return row.timezone or row.org_timezone or DEFAULT_TIMEZONE


async def get_org_timezone(db: AsyncSession, organization_id: UUID) -> str:
    """조직의 타임존을 반환합니다.

    Get the organization's timezone.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        organization_id: 조직 UUID (Organization UUID)

    Returns:
        str: IANA 타임존 문자열 (IANA timezone string)
    """
    result = await db.execute(
        select(Organization.timezone).where(Organization.id == organization_id)
    )
    tz = result.scalar_one_or_none()
    return tz or DEFAULT_TIMEZONE


def resolve_timezone(client_timezone: str | None, store_timezone: str) -> str:
    """클라이언트 타임존과 매장 타임존 중 유효한 값을 반환합니다.

    Resolve effective timezone: client override → store/org default.

    Args:
        client_timezone: 클라이언트가 전송한 타임존 (Client-sent timezone, may be None)
        store_timezone: 매장/조직 타임존 (Store/org timezone)

    Returns:
        str: 유효한 IANA 타임존 (Effective IANA timezone)
    """
    return client_timezone or store_timezone
