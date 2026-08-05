"""org_numbering.next_empid 계약 테스트 — 그룹 채번 스코프/번호대/정책 A.

각 테스트는 uuid4 suffix 로 고유한 org/store/user 를 새로 만들어 격리한다
(실데이터가 있는 워크트리 DB 에서 돌아도 기존 데이터에 의존/영향 없음).
org 삭제 CASCADE 로 roles/users/org_members/stores/store_groups 일괄 정리.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import (
    NUMBERING_MODE_GROUP,
    NUMBERING_MODE_STORE,
    Organization,
    Store,
    StoreGroup,
)
from app.models.user import Role, User
from app.services.org_numbering import ensure_member_store, remove_member_store

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 격리 시드 헬퍼 — 테스트마다 고유 org/role/store/user 직접 INSERT
# ---------------------------------------------------------------------------


class Ctx:
    """테스트 1개 전용 org 컨텍스트."""

    def __init__(self, org_id: UUID, role_id: UUID, sfx: str):
        self.org_id = org_id
        self.role_id = role_id
        self.sfx = sfx


@pytest_asyncio.fixture
async def ctx(db: AsyncSession) -> AsyncIterator[Ctx]:
    """고유 org + staff role 생성, 종료 시 org 삭제(CASCADE 정리)."""
    sfx = uuid4().hex[:8]
    org = Organization(name=f"__numtest_org_{sfx}__")
    db.add(org)
    await db.flush()
    role = Role(organization_id=org.id, name="staff", priority=40)
    db.add(role)
    await db.flush()
    org_id, role_id = org.id, role.id
    await db.commit()
    try:
        yield Ctx(org_id, role_id, sfx)
    finally:
        async with async_session() as s:
            await s.execute(delete(Organization).where(Organization.id == org_id))
            await s.commit()


async def _make_user(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    """user + org_member 생성 (username 전역 unique — sfx 필수)."""
    user = User(
        organization_id=ctx.org_id,
        role_id=ctx.role_id,
        username=f"__numtest_{tag}_{ctx.sfx}",
        full_name=f"Num Test {tag}",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(OrgMember(user_id=user.id, organization_id=ctx.org_id, role_id=ctx.role_id))
    await db.flush()
    return user.id


async def _make_store(
    db: AsyncSession,
    ctx: Ctx,
    tag: str,
    *,
    group_id: UUID | None = None,
    number_range_start: int | None = None,
) -> UUID:
    store = Store(
        organization_id=ctx.org_id,
        name=f"__numtest_store_{tag}_{ctx.sfx}__",
        timezone="UTC",
        group_id=group_id,
        number_range_start=number_range_start,
    )
    db.add(store)
    await db.flush()
    return store.id


async def _make_group(
    db: AsyncSession,
    ctx: Ctx,
    tag: str,
    *,
    numbering_mode: str = NUMBERING_MODE_GROUP,
    number_range_start: int | None = None,
) -> UUID:
    group = StoreGroup(
        organization_id=ctx.org_id,
        name=f"__numtest_group_{tag}_{ctx.sfx}__",
        numbering_mode=numbering_mode,
        number_range_start=number_range_start,
    )
    db.add(group)
    await db.flush()
    return group.id


async def _member_store_row(
    db: AsyncSession, user_id: UUID, store_id: UUID
) -> OrgMemberStore | None:
    return (
        await db.execute(
            select(OrgMemberStore)
            .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
            .where(OrgMember.user_id == user_id, OrgMemberStore.store_id == store_id)
        )
    ).scalar_one_or_none()


async def _empid(db: AsyncSession, user_id: UUID, store_id: UUID) -> int | None:
    row = await _member_store_row(db, user_id, store_id)
    assert row is not None, "org_member_stores row missing"
    return row.empid


async def _assign(db: AsyncSession, user_id: UUID, store_id: UUID) -> int | None:
    """ensure_member_store 후 부여된 empid 반환 (flush 로 다음 채번에 반영)."""
    await ensure_member_store(db, user_id, store_id)
    await db.flush()
    return await _empid(db, user_id, store_id)


# ---------------------------------------------------------------------------
# ① 그룹 없음 — 매장 단독 시퀀스 1,2,3 (기존 동작 회귀)
# ---------------------------------------------------------------------------


async def test_ungrouped_store_sequences_from_one(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "solo")
    users = [await _make_user(db, ctx, f"u{i}") for i in range(3)]
    got = [await _assign(db, u, store) for u in users]
    assert got == [1, 2, 3]
    await db.commit()


# ---------------------------------------------------------------------------
# ② numbering_mode="group" — 그룹 내 매장들이 시퀀스 공유 (매장 달라도 연번)
# ---------------------------------------------------------------------------


async def test_group_mode_shares_sequence_across_stores(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "shared", numbering_mode=NUMBERING_MODE_GROUP)
    store_a = await _make_store(db, ctx, "a", group_id=group)
    store_b = await _make_store(db, ctx, "b", group_id=group)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(4)]

    # A,B 번갈아 배정 → 1,2,3,4 연번
    got = [
        await _assign(db, users[0], store_a),
        await _assign(db, users[1], store_b),
        await _assign(db, users[2], store_a),
        await _assign(db, users[3], store_b),
    ]
    assert got == [1, 2, 3, 4]
    await db.commit()


# ---------------------------------------------------------------------------
# ③ numbering_mode="store" — 그룹 소속이어도 매장별 독립 (각자 1부터)
# ---------------------------------------------------------------------------


async def test_store_mode_group_keeps_independent_sequences(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "indep", numbering_mode=NUMBERING_MODE_STORE)
    store_a = await _make_store(db, ctx, "a", group_id=group)
    store_b = await _make_store(db, ctx, "b", group_id=group)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(3)]

    assert await _assign(db, users[0], store_a) == 1
    assert await _assign(db, users[1], store_b) == 1  # B 는 독립적으로 1부터
    assert await _assign(db, users[2], store_a) == 2
    await db.commit()


# ---------------------------------------------------------------------------
# ④ number_range_start — 그룹 1000 → 첫 배정 1000; 매장 override 300 우선
# ---------------------------------------------------------------------------


async def test_number_range_start_group_floor_and_store_override(
    db: AsyncSession, ctx: Ctx
) -> None:
    # 그룹 공유 + 그룹 번호대 1000 → 첫 배정 1000, 다음 매장이어도 1001 (공유 연번)
    shared = await _make_group(
        db, ctx, "floor", numbering_mode=NUMBERING_MODE_GROUP, number_range_start=1000
    )
    store_a = await _make_store(db, ctx, "a", group_id=shared)
    store_b = await _make_store(db, ctx, "b", group_id=shared)
    u1 = await _make_user(db, ctx, "u1")
    u2 = await _make_user(db, ctx, "u2")
    assert await _assign(db, u1, store_a) == 1000
    assert await _assign(db, u2, store_b) == 1001

    # 매장 override — 그룹 1000 이어도 매장 number_range_start=300 이 우선
    indep = await _make_group(
        db, ctx, "ovr", numbering_mode=NUMBERING_MODE_STORE, number_range_start=1000
    )
    store_c = await _make_store(db, ctx, "c", group_id=indep, number_range_start=300)
    store_d = await _make_store(db, ctx, "d", group_id=indep)
    u3 = await _make_user(db, ctx, "u3")
    u4 = await _make_user(db, ctx, "u4")
    assert await _assign(db, u3, store_c) == 300   # store 값이 그룹값보다 우선
    assert await _assign(db, u4, store_d) == 1000  # override 없는 매장은 그룹 폴백
    await db.commit()


async def test_shared_group_ignores_store_range_start(db: AsyncSession, ctx: Ctx) -> None:
    """Shared 그룹은 그룹 대역만 사용 — 매장 개별 range 무시 (QA 발견 회귀).

    과거 Per-store 시절 남은 매장값(예: 700)이 Shared 공유 시퀀스를
    엉뚱한 대역으로 밀어올리면 안 된다.
    """
    shared = await _make_group(
        db, ctx, "sifloor", numbering_mode=NUMBERING_MODE_GROUP, number_range_start=300
    )
    # 매장에 잔존 개별값 700 — Shared 모드에선 무시되어야 함
    store_a = await _make_store(db, ctx, "sa", group_id=shared, number_range_start=700)
    store_b = await _make_store(db, ctx, "sb", group_id=shared)
    u1 = await _make_user(db, ctx, "s1")
    u2 = await _make_user(db, ctx, "s2")
    assert await _assign(db, u1, store_a) == 300  # 700 아님 — 그룹 대역
    assert await _assign(db, u2, store_b) == 301  # 공유 연번
    await db.commit()


# ---------------------------------------------------------------------------
# ⑤ 한 사람이 공유 그룹의 A,B 두 매장 배정 → empid 행 2개, 서로 다른 값
# ---------------------------------------------------------------------------


async def test_same_person_two_stores_in_shared_group_gets_two_empids(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = await _make_group(db, ctx, "dual", numbering_mode=NUMBERING_MODE_GROUP)
    store_a = await _make_store(db, ctx, "a", group_id=group)
    store_b = await _make_store(db, ctx, "b", group_id=group)
    user = await _make_user(db, ctx, "dual")

    empid_a = await _assign(db, user, store_a)
    empid_b = await _assign(db, user, store_b)
    assert empid_a == 1 and empid_b == 2
    assert empid_a != empid_b  # 행 2개, 값 상이 (공유 시퀀스라 연번)
    await db.commit()


# ---------------------------------------------------------------------------
# ⑥ 정책 A 회귀 — 해제(휴면) 후 재배정 시 같은 empid 복귀, 휴면 번호는 점유 유지
# ---------------------------------------------------------------------------


async def test_policy_a_unassign_then_reassign_restores_same_empid(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "pol")
    u = await _make_user(db, ctx, "u")
    v = await _make_user(db, ctx, "v")
    w = await _make_user(db, ctx, "w")

    assert await _assign(db, u, store) == 1
    assert await _assign(db, v, store) == 2

    # 해제 — 행 삭제가 아니라 휴면(플래그 false), empid 보존
    await remove_member_store(db, u, store)
    await db.flush()
    dormant = await _member_store_row(db, u, store)
    assert dormant is not None
    assert dormant.is_work_assignment is False and dormant.is_manager is False
    assert dormant.empid == 1

    # 휴면 번호도 점유 → 신규는 1 재사용 없이 MAX+1
    assert await _assign(db, w, store) == 3

    # 재배정 → 같은 행 재사용, 같은 empid 복귀 + 플래그 복구
    await ensure_member_store(db, u, store)
    await db.flush()
    restored = await _member_store_row(db, u, store)
    assert restored is not None
    assert restored.empid == 1
    assert restored.is_work_assignment is True
    await db.commit()
