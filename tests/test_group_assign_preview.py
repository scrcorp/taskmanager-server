"""store_group_service.assign_preview 계약 테스트 — 편입 미리보기 (읽기 전용).

test_org_numbering.py 의 격리 스타일 재사용: 각 테스트가 uuid4 suffix 로 고유한
org/store/user 를 새로 만들어 실데이터가 있는 워크트리 DB 에서도 안전하게 돈다.
org 삭제 CASCADE 로 roles/users/org_members/stores/store_groups 일괄 정리.

케이스:
    ① mode="group" 충돌 감지 (휴면 보유자 포함 — 번호 점유 유지 정책 미러)
    ② 같은 사람 같은 번호 → 충돌 아님 (정상)
    ③ 같은 사람 다른 번호 → person_split
    ④ mode="store" → 빈 배열 (독립 채번은 충돌 개념 없음)
    ⑤ group_id null(이탈) → 빈 배열
    ⑥ 타 org 매장/그룹 → 404 (존재 누설 방지)
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
from app.services.store_group_service import store_group_service
from app.utils.exceptions import NotFoundError

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
    org = Organization(name=f"__prevtest_org_{sfx}__")
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
        username=f"__prevtest_{tag}_{ctx.sfx}",
        full_name=f"Prev Test {tag}",
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
) -> UUID:
    store = Store(
        organization_id=ctx.org_id,
        name=f"__prevtest_store_{tag}_{ctx.sfx}__",
        timezone="UTC",
        group_id=group_id,
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
) -> UUID:
    group = StoreGroup(
        organization_id=ctx.org_id,
        name=f"__prevtest_group_{tag}_{ctx.sfx}__",
        numbering_mode=numbering_mode,
    )
    db.add(group)
    await db.flush()
    return group.id


async def _assign_empid(
    db: AsyncSession,
    user_id: UUID,
    store_id: UUID,
    empid: int,
    *,
    dormant: bool = False,
) -> None:
    """org_member_stores 행을 명시적 empid 로 시드 (dormant=휴면 — 번호 점유 유지)."""
    member_id = (
        await db.execute(select(OrgMember.id).where(OrgMember.user_id == user_id))
    ).scalar_one()
    db.add(
        OrgMemberStore(
            org_member_id=member_id,
            store_id=store_id,
            empid=empid,
            is_work_assignment=not dormant,
        )
    )
    await db.flush()


# ---------------------------------------------------------------------------
# ① mode="group" — 편입 멤버의 번호를 그룹 내 다른 사람이 사용 중이면 충돌
# ---------------------------------------------------------------------------


async def test_group_mode_detects_conflicts(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "shared", numbering_mode=NUMBERING_MODE_GROUP)
    store_t = await _make_store(db, ctx, "t", group_id=group)
    store_s = await _make_store(db, ctx, "s")  # 미그룹 — 편입 예정
    alice = await _make_user(db, ctx, "alice")
    bob = await _make_user(db, ctx, "bob")

    # Bob 이 그룹 매장 T 에서 #7 보유 — 휴면이어도 번호는 점유 (정책 A 미러)
    await _assign_empid(db, bob, store_t, 7, dormant=True)
    # Alice 는 편입 매장 S 에서 #7 보유
    await _assign_empid(db, alice, store_s, 7)
    await db.commit()

    preview = await store_group_service.assign_preview(db, ctx.org_id, store_s, group)
    assert preview.numbering_mode == NUMBERING_MODE_GROUP
    assert preview.incoming_with_empid == 1
    assert preview.person_splits == []
    assert len(preview.conflicts) == 1
    conflict = preview.conflicts[0]
    assert conflict.empid == 7
    assert conflict.incoming.user_id == str(alice)
    assert conflict.incoming.name == "Prev Test alice"
    assert [h.user_id for h in conflict.holders] == [str(bob)]
    assert conflict.holders[0].store_id == str(store_t)


# ---------------------------------------------------------------------------
# ② 같은 사람이 같은 번호 — 충돌도 분열도 아님 (정상)
# ---------------------------------------------------------------------------


async def test_same_person_same_number_is_not_conflict(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = await _make_group(db, ctx, "same", numbering_mode=NUMBERING_MODE_GROUP)
    store_t = await _make_store(db, ctx, "t", group_id=group)
    store_s = await _make_store(db, ctx, "s")
    dual = await _make_user(db, ctx, "dual")

    await _assign_empid(db, dual, store_t, 7)
    await _assign_empid(db, dual, store_s, 7)
    await db.commit()

    preview = await store_group_service.assign_preview(db, ctx.org_id, store_s, group)
    assert preview.conflicts == []
    assert preview.person_splits == []
    assert preview.incoming_with_empid == 1


# ---------------------------------------------------------------------------
# ③ 같은 사람이 다른 번호 — person_split (충돌은 아님)
# ---------------------------------------------------------------------------


async def test_person_split_detected(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "split", numbering_mode=NUMBERING_MODE_GROUP)
    store_t = await _make_store(db, ctx, "t", group_id=group)
    store_s = await _make_store(db, ctx, "s")
    dual = await _make_user(db, ctx, "dual")

    await _assign_empid(db, dual, store_t, 12)
    await _assign_empid(db, dual, store_s, 7)
    await db.commit()

    preview = await store_group_service.assign_preview(db, ctx.org_id, store_s, group)
    assert preview.conflicts == []
    assert len(preview.person_splits) == 1
    split = preview.person_splits[0]
    assert split.user_id == str(dual)
    assert split.incoming_empid == 7
    assert [(e.store_id, e.empid) for e in split.elsewhere] == [(str(store_t), 12)]


# ---------------------------------------------------------------------------
# ④ mode="store" — 독립 채번이라 같은 번호여도 충돌 개념 없음
# ---------------------------------------------------------------------------


async def test_store_mode_group_returns_empty(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "indep", numbering_mode=NUMBERING_MODE_STORE)
    store_t = await _make_store(db, ctx, "t", group_id=group)
    store_s = await _make_store(db, ctx, "s")
    alice = await _make_user(db, ctx, "alice")
    bob = await _make_user(db, ctx, "bob")

    await _assign_empid(db, bob, store_t, 7)
    await _assign_empid(db, alice, store_s, 7)
    await db.commit()

    preview = await store_group_service.assign_preview(db, ctx.org_id, store_s, group)
    assert preview.numbering_mode == NUMBERING_MODE_STORE
    assert preview.conflicts == []
    assert preview.person_splits == []
    assert preview.incoming_with_empid == 1


# ---------------------------------------------------------------------------
# ⑤ group_id null — 그룹 이탈 미리보기, 충돌 개념 없음
# ---------------------------------------------------------------------------


async def test_null_group_returns_empty(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "leave", numbering_mode=NUMBERING_MODE_GROUP)
    store_s = await _make_store(db, ctx, "s", group_id=group)
    alice = await _make_user(db, ctx, "alice")
    await _assign_empid(db, alice, store_s, 7)
    await db.commit()

    preview = await store_group_service.assign_preview(db, ctx.org_id, store_s, None)
    assert preview.numbering_mode is None
    assert preview.conflicts == []
    assert preview.person_splits == []
    assert preview.incoming_with_empid == 1


# ---------------------------------------------------------------------------
# ⑥ 타 org 매장/그룹 — 404 (존재 누설 방지, _validate_group_org 미러)
# ---------------------------------------------------------------------------


async def test_cross_org_store_or_group_raises_not_found(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = await _make_group(db, ctx, "mine", numbering_mode=NUMBERING_MODE_GROUP)
    store_s = await _make_store(db, ctx, "s")
    await db.commit()

    from app.utils.exceptions import AppError

    other_org = uuid4()  # 존재하지 않는 org 로 호출 = 타 org 관점과 동일
    with pytest.raises(AppError) as exc1:
        await store_group_service.assign_preview(db, other_org, store_s, group)
    assert exc1.value.status_code == 404

    fake_store = uuid4()
    with pytest.raises(AppError) as exc2:
        await store_group_service.assign_preview(db, ctx.org_id, fake_store, group)
    assert exc2.value.status_code == 404
