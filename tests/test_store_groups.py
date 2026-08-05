"""StoreGroup 스키마/서비스 회귀 테스트.

- 잘못된 UUID 가 Pydantic 경계에서 422(ValidationError)로 걸리는지 (수동 UUID() 변환의 500 방지)
- PUT 에서 NOT NULL 컬럼(name/numbering_mode)에 명시적 null → no-op (500 방지),
  nullable 인 number_range_start 는 명시적 null 로 해제 가능
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.organization import Organization, StoreGroup
from app.schemas.organization import (
    StoreCreate,
    StoreGroupCreate,
    StoreGroupReorderRequest,
    StoreGroupUpdate,
    StoreUpdate,
)
from app.services.store_group_service import store_group_service


# ---------------------------------------------------------------------------
# 스키마 — 잘못된 UUID/모드는 Pydantic 에서 걸린다 (라우터 도달 전 422)
# ---------------------------------------------------------------------------


def test_store_schemas_reject_malformed_uuid() -> None:
    with pytest.raises(ValidationError):
        StoreCreate(name="X", group_id="not-a-uuid")
    with pytest.raises(ValidationError):
        StoreUpdate(group_id="also-bad")
    with pytest.raises(ValidationError):
        StoreGroupReorderRequest(group_ids=["nope"])
    # 유효한 문자열 UUID 는 UUID 객체로 파싱된다
    gid = uuid4()
    assert StoreCreate(name="X", group_id=str(gid)).group_id == gid
    assert StoreUpdate(group_id=None).group_id is None


def test_store_group_schemas_validate_numbering_mode() -> None:
    with pytest.raises(ValidationError):
        StoreGroupCreate(name="G", numbering_mode="banana")
    assert StoreGroupCreate(name="G").numbering_mode == "group"
    assert StoreGroupUpdate(numbering_mode="STORE ").numbering_mode == "store"


# ---------------------------------------------------------------------------
# 서비스 — 명시적 null name/numbering_mode 는 no-op, number_range_start 는 해제
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def org_id(db: AsyncSession) -> AsyncIterator[UUID]:
    org = Organization(name=f"__sgtest_org_{uuid4().hex[:8]}__")
    db.add(org)
    await db.commit()
    oid = org.id
    try:
        yield oid
    finally:
        async with async_session() as s:
            await s.execute(delete(Organization).where(Organization.id == oid))
            await s.commit()


async def test_update_group_explicit_nulls(db: AsyncSession, org_id: UUID) -> None:
    group = StoreGroup(
        organization_id=org_id, name=f"__sgtest_grp_{uuid4().hex[:8]}__",
        numbering_mode="group", number_range_start=1000,
    )
    db.add(group)
    await db.commit()
    original_name = group.name

    # 명시적 null name/numbering_mode → NOT NULL 위반 500 이 아니라 no-op
    res = await store_group_service.update_group(
        db, group.id, org_id,
        StoreGroupUpdate.model_validate({"name": None, "numbering_mode": None}),
    )
    assert res.name == original_name
    assert res.numbering_mode == "group"
    assert res.number_range_start == 1000

    # nullable 인 number_range_start 는 명시적 null 로 해제 가능
    res2 = await store_group_service.update_group(
        db, group.id, org_id,
        StoreGroupUpdate.model_validate({"number_range_start": None}),
    )
    assert res2.number_range_start is None
