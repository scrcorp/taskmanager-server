"""export_naming_service — 매장 필터 → 파일명 스코프 조각.

파일명이 "어디 것인지" 를 담아야 폴더에 쌓였을 때 구분된다. 매장 1개/2개/
여러 개/전체의 표기가 엔드포인트마다 갈라지지 않도록 한 곳에서 검증한다.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.organization import Organization, Store
from app.services.export_naming_service import ALL_STORES, resolve_store_scope


class Ctx:
    def __init__(self, org_id: UUID, store_ids: list[UUID]):
        self.org_id = org_id
        self.store_ids = store_ids  # 이름 오름차순: 강남 / 서울 2호점 / Downtown


@pytest_asyncio.fixture
async def ctx(db: AsyncSession) -> AsyncIterator[Ctx]:
    """고유 org + 매장 3개(한글 포함). 종료 시 org 삭제(CASCADE)."""
    org = Organization(name=f"__scopetest_org_{uuid4().hex[:8]}__")
    db.add(org)
    await db.flush()
    stores = [
        Store(organization_id=org.id, name=name)
        for name in ("Downtown", "강남", "서울 2호점")
    ]
    db.add_all(stores)
    await db.flush()
    org_id = org.id
    # 서비스가 이름 오름차순으로 읽으므로 같은 순서로 보관
    ids = [s.id for s in sorted(stores, key=lambda s: s.name)]
    await db.commit()
    try:
        yield Ctx(org_id, ids)
    finally:
        async with async_session() as s:
            await s.execute(delete(Organization).where(Organization.id == org_id))
            await s.commit()


async def test_no_filter_is_all_stores(db: AsyncSession, ctx: Ctx) -> None:
    assert await resolve_store_scope(db, ctx.org_id, None) == ALL_STORES
    assert await resolve_store_scope(db, ctx.org_id, []) == ALL_STORES


async def test_single_store_uses_its_name(db: AsyncSession, ctx: Ctx) -> None:
    scope = await resolve_store_scope(db, ctx.org_id, [ctx.store_ids[0]])
    assert scope == "Downtown"


async def test_single_store_keeps_korean_name(db: AsyncSession, ctx: Ctx) -> None:
    """한글 매장명은 그대로 — 전송은 Content-Disposition 의 filename* 담당."""
    korean = [i for i in ctx.store_ids if i != ctx.store_ids[0]]
    scope = await resolve_store_scope(db, ctx.org_id, [korean[0]])
    assert scope == "강남"


async def test_two_stores_are_joined(db: AsyncSession, ctx: Ctx) -> None:
    scope = await resolve_store_scope(db, ctx.org_id, ctx.store_ids[:2])
    assert scope == "Downtown_강남"


async def test_three_or_more_stores_collapse(db: AsyncSession, ctx: Ctx) -> None:
    """이름을 다 붙이면 파일명이 못 쓰게 길어진다 — 첫 이름 + 나머지 개수."""
    scope = await resolve_store_scope(db, ctx.org_id, ctx.store_ids)
    assert scope == "Downtown+2"


async def test_other_org_store_is_ignored(db: AsyncSession, ctx: Ctx) -> None:
    """org 밖 id 는 이름을 못 읽는다 — 파일명 때문에 export 가 깨지면 안 된다."""
    scope = await resolve_store_scope(db, ctx.org_id, [uuid4()])
    assert scope == ALL_STORES
