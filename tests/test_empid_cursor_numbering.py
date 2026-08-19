"""empid 채번 커서 코어(S2) 계약 테스트 — RULE-A(발급) · RULE-B(편입 승격) · RULE-C(재계산).

이 파일이 회귀 방지선이다. 커서를 도입한 이유가 전부 여기에 케이스로 박혀 있다:

    - 예외 번호(대역 밖 수동 번호)가 순번을 끌고 올라가지 않는다
    - 번호를 가장 많이 가진 매장이 그룹을 떠나도 다음 사람이 남의 번호를 다시 받지 않는다
    - 이탈했다 재합류해도 기존 인원 번호와 부딪히지 않는다

각 테스트는 uuid4 suffix 로 고유한 org/store/user 를 새로 만들어 격리한다
(test_org_numbering.py 와 같은 방식 — 실데이터 워크트리 DB 에서도 안전).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
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
from app.services.org_numbering import (
    EMPID_SCOPE_GROUP,
    EMPID_SCOPE_STORE,
    duplicate_empids_in_scope,
    empid_cursor_scope,
    empid_cursor_state,
    ensure_member_store,
    next_empid,
    promote_group_cursor,
    recalculate_empid_cursor,
    remove_member_store,
    set_empid_cursor,
)

pytestmark = pytest.mark.asyncio


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
    org = Organization(name=f"__curnum_org_{sfx}__")
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


@pytest_asyncio.fixture(autouse=True)
async def _release_db(db: AsyncSession, ctx: Ctx) -> AsyncIterator[None]:
    """테스트가 실패해도 db 세션의 트랜잭션을 반드시 닫는다.

    발급 경로가 커서 행을 FOR UPDATE 로 잠그므로, 열린 트랜잭션이 남으면 ctx 정리의
    org DELETE 가 그 락에 걸려 멈춘다(실패 원인이 타임아웃에 묻힌다).
    ctx 뒤에 설정되므로 ctx teardown 보다 먼저 돌아간다.
    """
    yield
    await db.rollback()


async def _make_user(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    """user + org_member 생성 (username 전역 unique — sfx 필수)."""
    user = User(
        organization_id=ctx.org_id,
        role_id=ctx.role_id,
        username=f"__curnum_{tag}_{ctx.sfx}",
        full_name=f"Cursor Num {tag}",
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
    next_empid_cursor: int | None = None,
) -> UUID:
    store = Store(
        organization_id=ctx.org_id,
        name=f"__curnum_store_{tag}_{ctx.sfx}__",
        timezone="UTC",
        group_id=group_id,
        number_range_start=number_range_start,
        next_empid=next_empid_cursor,
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
    next_empid_cursor: int | None = None,
) -> UUID:
    group = StoreGroup(
        organization_id=ctx.org_id,
        name=f"__curnum_group_{tag}_{ctx.sfx}__",
        numbering_mode=numbering_mode,
        number_range_start=number_range_start,
        next_empid=next_empid_cursor,
    )
    db.add(group)
    await db.flush()
    return group.id


async def _row(db: AsyncSession, user_id: UUID, store_id: UUID) -> OrgMemberStore | None:
    return (
        await db.execute(
            select(OrgMemberStore)
            .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
            .where(OrgMember.user_id == user_id, OrgMemberStore.store_id == store_id)
        )
    ).scalar_one_or_none()


async def _assign(db: AsyncSession, user_id: UUID, store_id: UUID) -> int | None:
    """ensure_member_store 후 부여된 empid 반환 (flush 로 다음 채번에 반영)."""
    await ensure_member_store(db, user_id, store_id)
    await db.flush()
    row = await _row(db, user_id, store_id)
    assert row is not None, "org_member_stores row missing"
    return row.empid


async def _write_empid(
    db: AsyncSession, ctx: Ctx, tag: str, store_id: UUID, empid: int, kind: str
) -> UUID:
    """수동 기입 흉내 — 배정 행을 지정 번호/구분으로 직접 만든다(커서는 건드리지 않는다)."""
    user_id = await _make_user(db, ctx, tag)
    member_id = (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.user_id == user_id, OrgMember.organization_id == ctx.org_id
            )
        )
    ).scalar_one()
    db.add(OrgMemberStore(
        org_member_id=member_id, store_id=store_id, empid=empid, empid_kind=kind
    ))
    await db.flush()
    return user_id


async def _store_cursor(db: AsyncSession, store_id: UUID) -> int | None:
    return (
        await db.execute(select(Store.next_empid).where(Store.id == store_id))
    ).scalar_one()


async def _group_cursor(db: AsyncSession, group_id: UUID) -> int | None:
    return (
        await db.execute(select(StoreGroup.next_empid).where(StoreGroup.id == group_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# ① 빈 스코프 첫 발급 = 시작값
# ---------------------------------------------------------------------------


async def test_first_issue_on_empty_scope_uses_cursor_start(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "empty", next_empid_cursor=3000)
    u = await _make_user(db, ctx, "u")
    assert await _assign(db, u, store) == 3000
    assert await _store_cursor(db, store) == 3001
    await db.rollback()


async def test_first_issue_without_cursor_initialises_from_floor(
    db: AsyncSession, ctx: Ctx
) -> None:
    """커서 미초기화(도입 후 새로 만든 매장) — floor 로 최초 1회 초기화. MAX 폴백이 아니다."""
    store = await _make_store(db, ctx, "newstore", number_range_start=500)
    u = await _make_user(db, ctx, "u")
    assert await _store_cursor(db, store) is None
    assert await _assign(db, u, store) == 500
    assert await _store_cursor(db, store) == 501
    await db.rollback()


# ---------------------------------------------------------------------------
# ② 연속 발급 — 커서 전진, 구멍 없음
# ---------------------------------------------------------------------------


async def test_consecutive_issues_advance_cursor_without_gaps(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "seq", next_empid_cursor=1000)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(4)]
    got = [await _assign(db, u, store) for u in users]
    assert got == [1000, 1001, 1002, 1003]
    assert await _store_cursor(db, store) == 1004
    await db.rollback()


async def test_group_scope_shares_one_cursor_across_stores(
    db: AsyncSession, ctx: Ctx
) -> None:
    """Shared 그룹 — 매장이 달라도 그룹 커서 하나로 연번. 매장 커서는 건드리지 않는다."""
    group = await _make_group(db, ctx, "shared", next_empid_cursor=7040)
    store_a = await _make_store(db, ctx, "a", group_id=group, next_empid_cursor=1)
    store_b = await _make_store(db, ctx, "b", group_id=group, next_empid_cursor=1)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(4)]

    got = [
        await _assign(db, users[0], store_a),
        await _assign(db, users[1], store_b),
        await _assign(db, users[2], store_a),
        await _assign(db, users[3], store_b),
    ]
    assert got == [7040, 7041, 7042, 7043]
    assert await _group_cursor(db, group) == 7044
    assert await _store_cursor(db, store_a) == 1  # 쉬고 있는 매장 커서는 불변
    await db.rollback()


# ---------------------------------------------------------------------------
# ③ 예외 번호 기입 후 발급 — 커서 불변, 예외에 안 끌림 (커서를 도입한 첫 번째 이유)
# ---------------------------------------------------------------------------


async def test_exception_empid_does_not_drag_the_sequence(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "exc", next_empid_cursor=1010)
    # 본사 이관 인원에게 대역 밖 6012 를 수동 기입 — 커서는 전진하지 않는다(INV-5)
    await _write_empid(db, ctx, "hq", store, 6012, EMPID_KIND_EXCEPTION)
    assert await _store_cursor(db, store) == 1010

    u = await _make_user(db, ctx, "next")
    assert await _assign(db, u, store) == 1010  # 6013 이 아니다 (구 MAX+1 이면 6013)
    assert await _store_cursor(db, store) == 1011
    await db.rollback()


async def test_manual_sequence_write_also_leaves_cursor_alone(
    db: AsyncSession, ctx: Ctx
) -> None:
    """예외가 아니라 순번으로 수동 기입해도 커서는 안 움직인다 (INV-5). 정정은 재계산으로."""
    store = await _make_store(db, ctx, "manual", next_empid_cursor=100)
    await _write_empid(db, ctx, "m", store, 250, EMPID_KIND_SEQUENCE)
    assert await _store_cursor(db, store) == 100
    state = await empid_cursor_state(db, store_id=store)
    assert (state.recommended, state.mismatch) == (251, True)  # RULE-E — 정정이 필요하다
    await db.rollback()


# ---------------------------------------------------------------------------
# ④ MAX 보유 매장 이탈 후 발급 — 커서 불변, 재발급 없음 (두 번째 이유)
# ---------------------------------------------------------------------------


async def test_leaving_store_with_max_empid_does_not_rewind_group_cursor(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = await _make_group(db, ctx, "leave", next_empid_cursor=1)
    store_a = await _make_store(db, ctx, "a", group_id=group, next_empid_cursor=1)
    store_b = await _make_store(db, ctx, "b", group_id=group, next_empid_cursor=1)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(3)]

    assert await _assign(db, users[0], store_a) == 1
    assert await _assign(db, users[1], store_b) == 2
    assert await _assign(db, users[2], store_b) == 3   # 최대 번호는 B 가 보유
    assert await _group_cursor(db, group) == 4

    # B 가 그룹을 떠난다 — 그룹 스코프에서 2,3 이 사라져도 커서는 그대로 4
    store_b_row = await db.get(Store, store_b)
    store_b_row.group_id = None
    await db.flush()
    assert await _group_cursor(db, group) == 4

    u = await _make_user(db, ctx, "after")
    assert await _assign(db, u, store_a) == 4  # 구 MAX+1 이면 2 — 남의 번호 재발급
    await db.rollback()


# ---------------------------------------------------------------------------
# ⑤ 이탈 후 재합류 — 기존 인원 번호와 충돌 없음
# ---------------------------------------------------------------------------


async def test_rejoining_store_does_not_collide_with_existing_numbers(
    db: AsyncSession, ctx: Ctx
) -> None:
    """이탈 → 재합류해도 그룹 커서는 되돌아가지 않는다 → 신규가 기존 번호를 다시 받지 않는다.

    떨어져 있는 동안 매장이 단독 커서로 발급한 번호는 재합류 시 그룹 안에서 중복이 될 수
    있다 — 정책 A 대로 자동 재번호는 하지 않고 duplicate_empids_in_scope 가 경고한다
    (해소는 EMPID 임포트). 커서가 지켜야 하는 건 "신규 발급이 남의 번호를 안 준다" 쪽이다.
    """
    group = await _make_group(db, ctx, "rejoin", next_empid_cursor=1)
    store_a = await _make_store(db, ctx, "a", group_id=group, next_empid_cursor=1)
    store_b = await _make_store(db, ctx, "b", group_id=group, next_empid_cursor=1)
    users = [await _make_user(db, ctx, f"u{i}") for i in range(3)]

    assert await _assign(db, users[0], store_a) == 1
    assert await _assign(db, users[1], store_b) == 2
    assert await _assign(db, users[2], store_b) == 3
    assert await _group_cursor(db, group) == 4

    # B 이탈 — 단독 스코프로 돌아가 자기 커서(1)에서 발급. 자기 매장 안에서는 유일하다.
    b_row = await db.get(Store, store_b)
    b_row.group_id = None
    await db.flush()
    solo = await _make_user(db, ctx, "solo")
    assert await _assign(db, solo, store_b) == 1  # store_b 안에서 1 은 비어 있다

    # 재합류 + 편입 승격 (RULE-B) — 커서끼리 비교라 그룹 커서는 4 에서 내려가지 않는다
    b_row.group_id = group
    await db.flush()
    await promote_group_cursor(db, group_id=group, store_id=store_b)
    await db.flush()
    assert await _group_cursor(db, group) == 4

    scope = await empid_cursor_scope(db, store_a)
    held = set(
        (
            await db.execute(
                select(OrgMemberStore.empid).where(
                    OrgMemberStore.store_id.in_(scope.store_ids),
                    OrgMemberStore.empid.isnot(None),
                )
            )
        ).scalars().all()
    )
    rejoined = await _make_user(db, ctx, "rejoined")
    got = await _assign(db, rejoined, store_a)
    assert got == 4 and got not in held, f"기존 인원 번호와 충돌: {got}"

    # 떨어져 있는 동안 생긴 중복(1)은 자동 재번호 대신 경고로 드러난다
    dups = await duplicate_empids_in_scope(db, scope.store_ids)
    assert {d["empid"] for d in dups} == {1}
    await db.rollback()


# ---------------------------------------------------------------------------
# ⑥ 커서가 점유 번호를 가리킴 — 건너뛰고 발급 + 커서 정정 (INV-3)
# ---------------------------------------------------------------------------


async def test_cursor_pointing_at_taken_number_skips_and_corrects(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "skip", next_empid_cursor=10)
    for i, value in enumerate((10, 11, 12)):
        await _write_empid(db, ctx, f"t{i}", store, value, EMPID_KIND_SEQUENCE)

    u = await _make_user(db, ctx, "u")
    assert await _assign(db, u, store) == 13  # 10,11,12 를 건너뛴다
    assert await _store_cursor(db, store) == 14  # 커서도 정정된다
    await db.rollback()


# ---------------------------------------------------------------------------
# ⑦ 편입 승격 — 커서끼리 비교(예외가 승격에 영향 없음) — RULE-B
# ---------------------------------------------------------------------------


async def test_promote_group_cursor_takes_max_of_cursors(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = await _make_group(db, ctx, "promo", next_empid_cursor=1000)
    joining = await _make_store(db, ctx, "join", next_empid_cursor=1500)

    promoted = await promote_group_cursor(db, group_id=group, store_id=joining)
    assert promoted == 1500
    assert await _group_cursor(db, group) == 1500
    await db.rollback()


async def test_promote_group_cursor_ignores_exception_empids(
    db: AsyncSession, ctx: Ctx
) -> None:
    """편입 매장의 예외 번호(9만번대)가 그룹 커서를 밀어올리면 안 된다. MAX+1 금지의 핵심."""
    group = await _make_group(db, ctx, "promo2", next_empid_cursor=1000)
    joining = await _make_store(db, ctx, "join2", next_empid_cursor=1200)
    await _write_empid(db, ctx, "hq", joining, 90001, EMPID_KIND_EXCEPTION)

    assert await promote_group_cursor(db, group_id=group, store_id=joining) == 1200
    assert await _group_cursor(db, group) == 1200  # 90002 가 아니다
    await db.rollback()


async def test_promote_group_cursor_never_lowers(db: AsyncSession, ctx: Ctx) -> None:
    """그룹이 이미 앞서 있으면 편입은 커서를 낮추지 않는다 (INV-2)."""
    group = await _make_group(db, ctx, "promo3", next_empid_cursor=8000)
    joining = await _make_store(db, ctx, "join3", next_empid_cursor=1200)
    assert await promote_group_cursor(db, group_id=group, store_id=joining) == 8000
    assert await _group_cursor(db, group) == 8000
    await db.rollback()


async def test_promote_group_cursor_logs_cursor_change(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "promo4", next_empid_cursor=1000)
    joining = await _make_store(db, ctx, "join4", next_empid_cursor=1500)
    await promote_group_cursor(db, group_id=group, store_id=joining, reason="joined group")
    await db.flush()
    change = (
        await db.execute(
            select(EmpidChange).where(
                EmpidChange.organization_id == ctx.org_id,
                EmpidChange.source == EMPID_SOURCE_CURSOR,
            )
        )
    ).scalar_one()
    assert (change.old_empid, change.new_empid) == (1000, 1500)
    assert change.user_id is None and change.reason == "joined group"
    await db.rollback()


# ---------------------------------------------------------------------------
# ⑧ 백필 후 첫 발급 = 마이그레이션 전 동작과 동일 (INV-9)
# ---------------------------------------------------------------------------


async def test_first_issue_after_backfill_matches_legacy_max_plus_one(
    db: AsyncSession, ctx: Ctx
) -> None:
    """백필 커서(= 구 next_empid 계산 결과)에서 발급하면 결과가 마이그레이션 전과 같다."""
    store = await _make_store(db, ctx, "bf")
    await _write_empid(db, ctx, "a", store, 1, EMPID_KIND_SEQUENCE)
    await _write_empid(db, ctx, "b", store, 7, EMPID_KIND_SEQUENCE)
    # 백필 = max(MAX(empid)+1, floor) — 마이그레이션이 채워 둔 값
    store_row = await db.get(Store, store)
    store_row.next_empid = 8
    await db.flush()

    u = await _make_user(db, ctx, "u")
    assert await _assign(db, u, store) == 8  # 구 MAX+1 과 동일
    await db.rollback()


async def test_first_issue_after_backfill_group_scope(db: AsyncSession, ctx: Ctx) -> None:
    group = await _make_group(db, ctx, "bfg", number_range_start=1000, next_empid_cursor=1043)
    store_a = await _make_store(db, ctx, "a", group_id=group, next_empid_cursor=1043)
    store_b = await _make_store(db, ctx, "b", group_id=group, next_empid_cursor=1043)
    await _write_empid(db, ctx, "g1", store_a, 1000, EMPID_KIND_SEQUENCE)
    await _write_empid(db, ctx, "g2", store_b, 1042, EMPID_KIND_SEQUENCE)

    u = await _make_user(db, ctx, "u")
    assert await _assign(db, u, store_a) == 1043
    await db.rollback()


# ---------------------------------------------------------------------------
# ⑨ 동시 발급 — 중복 없음
# ---------------------------------------------------------------------------


async def _issue_in_own_transaction(user_id: UUID, store_id: UUID) -> int | None:
    """별도 세션/트랜잭션에서 1건 발급 후 커밋 — 실제 동시 요청 흉내."""
    async with async_session() as s:
        await ensure_member_store(s, user_id, store_id)
        await s.flush()
        row = (
            await s.execute(
                select(OrgMemberStore.empid)
                .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
                .where(OrgMember.user_id == user_id, OrgMemberStore.store_id == store_id)
            )
        ).scalar_one()
        await s.commit()
        return row


async def test_concurrent_issue_store_scope_has_no_duplicates(
    db: AsyncSession, ctx: Ctx
) -> None:
    """매장 단독 스코프 — 커서 행 FOR UPDATE 가 직렬화 지점."""
    store = await _make_store(db, ctx, "conc", next_empid_cursor=1)
    users = [await _make_user(db, ctx, f"c{i}") for i in range(5)]
    await db.commit()

    got = await asyncio.gather(*[_issue_in_own_transaction(u, store) for u in users])
    assert sorted(got) == [1, 2, 3, 4, 5]
    async with async_session() as s:
        assert (
            await s.execute(select(Store.next_empid).where(Store.id == store))
        ).scalar_one() == 6


async def test_concurrent_issue_group_scope_has_no_duplicates(
    db: AsyncSession, ctx: Ctx
) -> None:
    """그룹 공유 스코프 — advisory lock + 커서 행 락. 매장이 달라도 중복이 없어야 한다."""
    group = await _make_group(db, ctx, "concg", next_empid_cursor=100)
    store_a = await _make_store(db, ctx, "a", group_id=group, next_empid_cursor=1)
    store_b = await _make_store(db, ctx, "b", group_id=group, next_empid_cursor=1)
    users = [await _make_user(db, ctx, f"cg{i}") for i in range(6)]
    await db.commit()

    stores = [store_a, store_b] * 3
    got = await asyncio.gather(
        *[_issue_in_own_transaction(u, s) for u, s in zip(users, stores)]
    )
    assert sorted(got) == [100, 101, 102, 103, 104, 105]


# ---------------------------------------------------------------------------
# 배정 해제(휴면) — 커서 불변 (INV-4)
# ---------------------------------------------------------------------------


async def test_remove_member_store_leaves_cursor_untouched(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "dormant", next_empid_cursor=1)
    u = await _make_user(db, ctx, "u")
    assert await _assign(db, u, store) == 1
    assert await _store_cursor(db, store) == 2

    await remove_member_store(db, u, store)
    await db.flush()
    assert await _store_cursor(db, store) == 2  # 반납도, 되돌림도 없다

    w = await _make_user(db, ctx, "w")
    assert await _assign(db, w, store) == 2  # 휴면 번호 1 은 여전히 점유
    await db.rollback()


# ---------------------------------------------------------------------------
# 스코프 판정 — API 가 그대로 쓰는 scope/scope_id (계약 §3-1)
# ---------------------------------------------------------------------------


async def test_cursor_scope_reports_holder(db: AsyncSession, ctx: Ctx) -> None:
    shared = await _make_group(db, ctx, "sc1", numbering_mode=NUMBERING_MODE_GROUP)
    per_store = await _make_group(db, ctx, "sc2", numbering_mode=NUMBERING_MODE_STORE)
    in_shared = await _make_store(db, ctx, "a", group_id=shared)
    in_per_store = await _make_store(db, ctx, "b", group_id=per_store)
    ungrouped = await _make_store(db, ctx, "c")

    s1 = await empid_cursor_scope(db, in_shared)
    assert (s1.scope, s1.scope_id) == (EMPID_SCOPE_GROUP, shared)
    s2 = await empid_cursor_scope(db, in_per_store)
    assert (s2.scope, s2.scope_id) == (EMPID_SCOPE_STORE, in_per_store)
    s3 = await empid_cursor_scope(db, ungrouped)
    assert (s3.scope, s3.scope_id) == (EMPID_SCOPE_STORE, ungrouped)
    await db.rollback()


# ---------------------------------------------------------------------------
# RULE-C 재계산 — sequence 한정 MAX, 예외 제외 건수 동반
# ---------------------------------------------------------------------------


async def test_recalculate_excludes_exceptions(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "recalc", next_empid_cursor=90002)
    await _write_empid(db, ctx, "s1", store, 1200, EMPID_KIND_SEQUENCE)
    await _write_empid(db, ctx, "s2", store, 1201, EMPID_KIND_SEQUENCE)
    await _write_empid(db, ctx, "e1", store, 90001, EMPID_KIND_EXCEPTION)

    state, previous = await recalculate_empid_cursor(db, store_id=store, apply=False)
    assert state.recommended == 1202  # 90002 아님 — 예외 제외
    assert (state.exception_count, state.sequence_count) == (1, 2)
    assert previous == 90002
    assert await _store_cursor(db, store) == 90002  # 미리보기는 적용하지 않는다
    await db.rollback()


async def test_recalculate_apply_writes_cursor_and_history(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "recalc2", next_empid_cursor=90002)
    await _write_empid(db, ctx, "s1", store, 1200, EMPID_KIND_SEQUENCE)
    await _write_empid(db, ctx, "e1", store, 90001, EMPID_KIND_EXCEPTION)

    state, previous = await recalculate_empid_cursor(
        db, store_id=store, apply=True, reason="cursor corrected after import"
    )
    await db.flush()
    assert (state.next_empid, previous) == (1201, 90002)  # 낮추는 적용도 허용 (INV-2 예외)
    assert await _store_cursor(db, store) == 1201
    change = (
        await db.execute(
            select(EmpidChange).where(
                EmpidChange.organization_id == ctx.org_id,
                EmpidChange.source == EMPID_SOURCE_CURSOR,
            )
        )
    ).scalar_one()
    assert (change.old_empid, change.new_empid) == (90002, 1201)
    assert change.reason == "cursor corrected after import"
    await db.rollback()


async def test_recalculate_respects_floor_on_empty_scope(
    db: AsyncSession, ctx: Ctx
) -> None:
    """인원이 없으면 floor — max(base+1, floor) 의 floor 쪽."""
    store = await _make_store(db, ctx, "floor", number_range_start=3000, next_empid_cursor=3000)
    state, _ = await recalculate_empid_cursor(db, store_id=store, apply=False)
    assert state.recommended == 3000
    assert state.mismatch is False
    await db.rollback()


async def test_cursor_state_by_group_id(db: AsyncSession, ctx: Ctx) -> None:
    """그룹 설정 화면 경로 — 매장 없이 group_id 로 조회."""
    group = await _make_group(db, ctx, "gstate", number_range_start=1000, next_empid_cursor=1005)
    store_a = await _make_store(db, ctx, "a", group_id=group)
    await _write_empid(db, ctx, "g1", store_a, 1004, EMPID_KIND_SEQUENCE)

    state = await empid_cursor_state(db, group_id=group)
    assert state.scope == EMPID_SCOPE_GROUP and state.scope_id == group
    assert (state.next_empid, state.recommended, state.mismatch) == (1005, 1005, False)
    assert state.as_dict()["scope_id"] == str(group)
    await db.rollback()


async def test_manual_cursor_adjustment_records_history(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "manualadj", next_empid_cursor=7050)
    scope = await empid_cursor_scope(db, store)
    previous = await set_empid_cursor(
        db, scope=scope, value=7044, reason="corrected after import"
    )
    await db.flush()
    assert previous == 7050
    assert await _store_cursor(db, store) == 7044
    change = (
        await db.execute(
            select(EmpidChange).where(
                EmpidChange.organization_id == ctx.org_id,
                EmpidChange.source == EMPID_SOURCE_CURSOR,
            )
        )
    ).scalar_one()
    assert (change.old_empid, change.new_empid, change.reason) == (
        7050, 7044, "corrected after import",
    )
    await db.rollback()
