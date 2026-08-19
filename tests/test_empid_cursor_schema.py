"""empid 채번 커서 스키마(S1) 계약 테스트 — 컬럼·기본값·백필.

검증 대상:
    - org_member_stores.empid_kind 기본값이 'sequence' (INV-6)
    - empid_changes.reason 저장 + source='cursor' 상수
    - 커서 백필이 **채번 스코프 단위**로 계산되고, 백필 후 첫 발급 결과가
      마이그레이션 전(next_empid() 의 MAX 계산)과 같다 (INV-9)

백필 SQL 은 마이그레이션 모듈에서 그대로 import 해 재실행한다 — 테스트가 검증하는
문장이 실제로 운영에 나가는 문장과 같아야 하기 때문.
각 테스트는 uuid4 suffix 로 고유 org/store/user 를 만들어 격리한다
(test_org_numbering.py 와 같은 방식 — 실데이터 워크트리 DB 에서도 안전).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.empid_change import EMPID_SOURCE_CURSOR, EmpidChange
from app.models.org_member import (
    EMPID_KIND_EXCEPTION,
    EMPID_KIND_SEQUENCE,
    OrgMember,
    OrgMemberStore,
)
from app.models.organization import (
    NUMBERING_MODE_GROUP,
    NUMBERING_MODE_STORE,
    Organization,
    Store,
    StoreGroup,
)
from app.models.user import Role, User
from app.services.org_numbering import _empid_floor, empid_scope_store_ids

pytestmark = pytest.mark.asyncio


def _load_migration():
    """백필 SQL 상수를 마이그레이션 파일에서 직접 로드 (versions 는 패키지가 아님)."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "f4a91c7b2e10_empid_cursor_kind_reason.py"
    )
    spec = importlib.util.spec_from_file_location("_empid_cursor_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


MIGRATION = _load_migration()


# ---------------------------------------------------------------------------
# 격리 시드 헬퍼
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
    org = Organization(name=f"__curtest_org_{sfx}__")
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


async def _make_member(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    """user + org_member 생성 후 org_member id 반환."""
    user = User(
        organization_id=ctx.org_id,
        role_id=ctx.role_id,
        username=f"__curtest_{tag}_{ctx.sfx}",
        full_name=f"Cursor Test {tag}",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    member = OrgMember(user_id=user.id, organization_id=ctx.org_id, role_id=ctx.role_id)
    db.add(member)
    await db.flush()
    return member.id


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
        name=f"__curtest_store_{tag}_{ctx.sfx}__",
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
        name=f"__curtest_group_{tag}_{ctx.sfx}__",
        numbering_mode=numbering_mode,
        number_range_start=number_range_start,
    )
    db.add(group)
    await db.flush()
    return group.id


async def _assign_empid(
    db: AsyncSession, ctx: Ctx, tag: str, store_id: UUID, empid: int | None,
    *, empid_kind: str | None = None,
) -> OrgMemberStore:
    """org_member_stores 행을 지정 empid 로 직접 생성 (백필 대상 기존 데이터 흉내)."""
    member_id = await _make_member(db, ctx, tag)
    kwargs = {} if empid_kind is None else {"empid_kind": empid_kind}
    row = OrgMemberStore(org_member_id=member_id, store_id=store_id, empid=empid, **kwargs)
    db.add(row)
    await db.flush()
    return row


async def _run_backfill(db: AsyncSession) -> None:
    """마이그레이션과 동일한 백필 SQL 재실행 (커서를 NULL 로 되돌린 뒤 호출)."""
    await db.execute(text(MIGRATION.BACKFILL_GROUP_CURSOR_SQL))
    await db.execute(text(MIGRATION.BACKFILL_STORE_CURSOR_SQL))
    await db.flush()


async def _store_cursor(db: AsyncSession, store_id: UUID) -> int | None:
    return (await db.execute(select(Store.next_empid).where(Store.id == store_id))).scalar_one()


async def _group_cursor(db: AsyncSession, group_id: UUID) -> int | None:
    return (
        await db.execute(select(StoreGroup.next_empid).where(StoreGroup.id == group_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# ① empid_kind — 기본값 sequence (INV-6)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _release_db(db: AsyncSession, ctx: Ctx) -> AsyncIterator[None]:
    """테스트가 실패해도 db 세션의 트랜잭션을 반드시 닫는다.

    assert 로 중단되면 마지막 줄 rollback 에 도달하지 못해 UPDATE 락이 열린 채 남고,
    ctx teardown 의 org DELETE 가 그 락을 무한 대기한다(파일 하나가 전체 런을 멈춘다).
    ctx 뒤에 설정되므로 ctx teardown 보다 먼저 돌아간다.
    """
    yield
    await db.rollback()


async def _legacy_next_empid(db: AsyncSession, store_id) -> int:
    """마이그레이션 **전** 채번식을 재현한다 — max(스코프 MAX, floor-1)+1.

    백필이 "기존 동작과 같은 다음 번호"를 넣었는지 검증하는 것이 목적이므로(INV-9),
    기대값은 옛 계산식이어야 한다. 현행 next_empid() 는 커서를 읽고 전진시키므로
    (INV-1: MAX 미사용) 오라클로 쓸 수 없다 — 쓰면 커서를 +1 밀어 테스트가 자기
    전제를 무너뜨린다.
    """
    scope_ids = await empid_scope_store_ids(db, store_id)
    floor = await _empid_floor(db, store_id)
    current_max = (
        await db.execute(
            select(func.coalesce(func.max(OrgMemberStore.empid), 0)).where(
                OrgMemberStore.store_id.in_(scope_ids)
            )
        )
    ).scalar() or 0
    return max(current_max, floor - 1) + 1


async def test_empid_kind_defaults_to_sequence(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "kind")
    row = await _assign_empid(db, ctx, "k1", store, 1)  # empid_kind 미지정
    await db.refresh(row)
    assert row.empid_kind == EMPID_KIND_SEQUENCE
    await db.rollback()


async def test_empid_kind_accepts_exception(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "kind2")
    row = await _assign_empid(db, ctx, "k2", store, 6012, empid_kind=EMPID_KIND_EXCEPTION)
    await db.refresh(row)
    assert row.empid_kind == EMPID_KIND_EXCEPTION
    await db.rollback()


# ---------------------------------------------------------------------------
# ② empid_changes.reason + source='cursor'
# ---------------------------------------------------------------------------


async def test_empid_change_reason_and_cursor_source(db: AsyncSession, ctx: Ctx) -> None:
    assert EMPID_SOURCE_CURSOR == "cursor"
    store = await _make_store(db, ctx, "hist")
    change = EmpidChange(
        organization_id=ctx.org_id,
        store_id=store,
        old_empid=7050,      # 커서 변경은 old/new 에 커서 값을 담는다
        new_empid=7044,
        source=EMPID_SOURCE_CURSOR,
        user_id=None,
        reason="cursor corrected after import",
    )
    db.add(change)
    await db.flush()
    await db.refresh(change)
    assert change.reason == "cursor corrected after import"
    assert change.source == EMPID_SOURCE_CURSOR
    await db.rollback()


async def test_empid_change_reason_is_optional(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "hist2")
    change = EmpidChange(
        organization_id=ctx.org_id, store_id=store,
        old_empid=None, new_empid=12, source="auto",
    )
    db.add(change)
    await db.flush()
    await db.refresh(change)
    assert change.reason is None
    await db.rollback()


# ---------------------------------------------------------------------------
# ③ 백필 — 스코프 단위 계산 + INV-9(백필 후 첫 발급 = 마이그레이션 전 동작)
# ---------------------------------------------------------------------------


async def test_backfill_ungrouped_store(db: AsyncSession, ctx: Ctx) -> None:
    """미그룹 매장 — 그 매장 MAX(empid)+1."""
    store = await _make_store(db, ctx, "solo")
    await _assign_empid(db, ctx, "s1", store, 1)
    await _assign_empid(db, ctx, "s2", store, 7)

    expected = await _legacy_next_empid(db, store)  # 마이그레이션 전 계산 = 8
    await _run_backfill(db)
    assert expected == 8
    assert await _store_cursor(db, store) == expected
    await db.rollback()


async def test_backfill_empty_store_uses_floor(db: AsyncSession, ctx: Ctx) -> None:
    """인원 없는 매장 — floor(매장 번호대 시작값)."""
    store = await _make_store(db, ctx, "empty", number_range_start=3000)
    expected = await _legacy_next_empid(db, store)
    await _run_backfill(db)
    assert expected == 3000
    assert await _store_cursor(db, store) == expected
    await db.rollback()


async def test_backfill_empty_scope_defaults_to_one(db: AsyncSession, ctx: Ctx) -> None:
    """번호대 미설정 + 인원 없음 — 1."""
    store = await _make_store(db, ctx, "blank")
    expected = await _legacy_next_empid(db, store)
    await _run_backfill(db)
    assert expected == 1
    assert await _store_cursor(db, store) == expected
    await db.rollback()


async def test_backfill_shared_group_scope(db: AsyncSession, ctx: Ctx) -> None:
    """Shared 그룹 — 그룹 커서 = 그룹 내 전 매장 MAX(empid)+1, floor 는 그룹 값."""
    group = await _make_group(
        db, ctx, "shared", numbering_mode=NUMBERING_MODE_GROUP, number_range_start=1000,
    )
    store_a = await _make_store(db, ctx, "a", group_id=group, number_range_start=5000)
    store_b = await _make_store(db, ctx, "b", group_id=group)
    await _assign_empid(db, ctx, "g1", store_a, 1000)
    await _assign_empid(db, ctx, "g2", store_b, 1042)

    expected = await _legacy_next_empid(db, store_a)  # 그룹 스코프 = 1043
    await _run_backfill(db)
    assert expected == 1043
    assert await _group_cursor(db, group) == expected
    # 매장 개별 커서도 채워진다 (그룹 이탈·모드 전환 대비 — NULL 폴백 제거)
    assert await _store_cursor(db, store_a) is not None
    assert await _store_cursor(db, store_b) is not None
    await db.rollback()


async def test_backfill_shared_group_floor_when_empty(db: AsyncSession, ctx: Ctx) -> None:
    """Shared 그룹 인원 없음 — 그룹 floor. 매장 개별 번호대는 그룹 커서에 영향 없음."""
    group = await _make_group(
        db, ctx, "sharedempty", numbering_mode=NUMBERING_MODE_GROUP, number_range_start=2000,
    )
    store = await _make_store(db, ctx, "a", group_id=group, number_range_start=9000)

    expected = await _legacy_next_empid(db, store)
    await _run_backfill(db)
    assert expected == 2000
    assert await _group_cursor(db, group) == expected
    await db.rollback()


async def test_backfill_per_store_group_scope(db: AsyncSession, ctx: Ctx) -> None:
    """Per-store 그룹 — 매장별 커서. floor 는 매장값 > 그룹값 > 1."""
    group = await _make_group(
        db, ctx, "perstore", numbering_mode=NUMBERING_MODE_STORE, number_range_start=100,
    )
    store_a = await _make_store(db, ctx, "a", group_id=group, number_range_start=500)
    store_b = await _make_store(db, ctx, "b", group_id=group)  # 그룹 기본값 100 상속
    await _assign_empid(db, ctx, "p1", store_a, 501)

    expected_a = await _legacy_next_empid(db, store_a)
    expected_b = await _legacy_next_empid(db, store_b)
    await _run_backfill(db)
    assert (expected_a, expected_b) == (502, 100)
    assert await _store_cursor(db, store_a) == expected_a
    assert await _store_cursor(db, store_b) == expected_b
    await db.rollback()


async def test_backfill_ignores_null_empid_rows(db: AsyncSession, ctx: Ctx) -> None:
    """empid NULL 배정은 백필 계산에 영향 없음."""
    store = await _make_store(db, ctx, "nulls")
    await _assign_empid(db, ctx, "n1", store, None)
    await _assign_empid(db, ctx, "n2", store, 4)

    expected = await _legacy_next_empid(db, store)
    await _run_backfill(db)
    assert expected == 5
    assert await _store_cursor(db, store) == expected
    await db.rollback()
