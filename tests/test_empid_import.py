"""empid_import_service 테스트 — preview 분류 + commit 3-phase 재기입.

각 테스트는 uuid4 suffix 로 고유한 org/store/user 를 새로 만들어 격리한다
(워크트리 DB 에 실데이터가 있어도 기존 데이터에 의존/영향 없음).
xlsx 는 openpyxl 인메모리 생성(tests/test_empid_reconcile.py 패턴).
"""

from __future__ import annotations

import io
from typing import AsyncIterator
from uuid import UUID, uuid4

import openpyxl
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Organization, Store, StoreGroup
from app.models.user import Role, User
from app.services import empid_import_service as svc
from app.services.empid_import_service import (
    ACTION_INVALID,
    ACTION_NEW_ASSIGNMENT,
    ACTION_REBIND,
    ACTION_SAME,
    ACTION_UNMATCHED_STORE,
    build_store_index,
    match_store,
)

# asyncio_mode=auto — async 테스트 자동 마킹 (파일 전역 마크는 sync 테스트에 경고 유발)


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
    org = Organization(name=f"__imptest_org_{sfx}__")
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


async def _make_user(db: AsyncSession, ctx: Ctx, tag: str, email: str) -> UUID:
    """user + org_member 생성 (username 전역 unique — sfx 필수)."""
    user = User(
        organization_id=ctx.org_id,
        role_id=ctx.role_id,
        username=f"__imptest_{tag}_{ctx.sfx}",
        full_name=f"Imp Test {tag}",
        password_hash="x",
        email=email,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(OrgMember(user_id=user.id, organization_id=ctx.org_id, role_id=ctx.role_id))
    await db.flush()
    return user.id


async def _make_store(
    db: AsyncSession, ctx: Ctx, tag: str, group_id: UUID | None = None
) -> UUID:
    """이름 = "IMPTEST STORE {tag} {sfx}" — COMPANY 정규화 매칭 대상."""
    store = Store(
        organization_id=ctx.org_id,
        name=f"IMPTEST STORE {tag} {ctx.sfx}",
        timezone="UTC",
        group_id=group_id,
    )
    db.add(store)
    await db.flush()
    return store.id


async def _member_id(db: AsyncSession, ctx: Ctx, user_id: UUID) -> UUID:
    return (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.user_id == user_id, OrgMember.organization_id == ctx.org_id
            )
        )
    ).scalar_one()


async def _give_empid(
    db: AsyncSession, ctx: Ctx, user_id: UUID, store_id: UUID, empid: int
) -> None:
    """기존 배정+번호 상태 시드 — org_member_stores 직접 INSERT."""
    db.add(
        OrgMemberStore(
            org_member_id=await _member_id(db, ctx, user_id),
            store_id=store_id,
            empid=empid,
        )
    )
    await db.flush()


async def _empids_in_store(db: AsyncSession, store_id: UUID) -> dict[UUID, int | None]:
    """{user_id: empid} — 검증용."""
    rows = (
        await db.execute(
            select(OrgMember.user_id, OrgMemberStore.empid)
            .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
            .where(OrgMemberStore.store_id == store_id)
        )
    ).all()
    return {r.user_id: r.empid for r in rows}


def _xlsx(rows: list[list]) -> bytes:
    """[[COMPANY, CORP_ABR_3, Name, emp_id, Email], ...] → xlsx bytes (인메모리)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["COMPANY", "CORP_ABR_3", "Name", "emp_id", "Email"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _entries_by_action(person) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in person.entries:
        out.setdefault(e.action, []).append(e)
    return out


# ---------------------------------------------------------------------------
# preview — (a) same / (b) rebind / (c) new_assignment / (d) unmatched / (e) invalid
# ---------------------------------------------------------------------------


async def test_preview_actions_same_rebind_new_unmatched_invalid(
    db: AsyncSession, ctx: Ctx
) -> None:
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    email1 = f"alice.{ctx.sfx}@example.com"
    email2 = f"bob.{ctx.sfx}@example.com"
    email3 = f"carol.{ctx.sfx}@example.com"
    u1 = await _make_user(db, ctx, "alice", email1)
    u2 = await _make_user(db, ctx, "bob", email2)
    u3 = await _make_user(db, ctx, "carol", email3)
    await _give_empid(db, ctx, u1, store_a, 5)   # (a) 업로드값 == 현재값
    await _give_empid(db, ctx, u2, store_a, 6)   # (b) 업로드값 != 현재값
    await db.commit()

    company_a = f"imptest-store-a-{ctx.sfx}"     # 정규화 매칭 (소문자/구분자 무시)
    company_b = f"IMPTEST STORE B {ctx.sfx}"
    content = _xlsx([
        [company_a, None, "ALICE KIM", "5", email1],                  # (a) same
        [f"ZZZ NOWHERE {ctx.sfx}", None, "ALICE KIM", "7", email1],   # (d) unmatched_store
        [company_a, None, "BOB LEE", "9", email2],                    # (b) rebind (6→9)
        [company_b, None, "BOB LEE", "ABC", email2],                  # (e) invalid (비정수)
        [company_b, None, "CAROL PARK", "4", email3],                 # (c) new_assignment
    ])

    res = await svc.preview(db, ctx.org_id, content, "list.xlsx")

    counts = res.counts()
    assert counts["people"] == 3
    assert counts["same"] == 1
    assert counts["rebind"] == 1
    assert counts["new_assignment"] == 1
    assert counts["unmatched_store"] == 1
    assert counts["invalid"] == 1
    assert not res.placeholder and not res.deferred

    by_email = {p.email: p for p in res.people}

    # (a) same — 현재값 5 == 업로드 5
    alice = _entries_by_action(by_email[email1])
    same = alice[ACTION_SAME][0]
    assert same.store_id == str(store_a)
    assert same.emp_id == 5 and same.current_empid == 5 and same.has_assignment

    # (d) unmatched_store — 매장 미매칭, store_id 없음
    unmatched = alice[ACTION_UNMATCHED_STORE][0]
    assert unmatched.store_id is None and unmatched.emp_id == 7

    # (b) rebind — current_empid 병기
    bob = _entries_by_action(by_email[email2])
    rebind = bob[ACTION_REBIND][0]
    assert rebind.store_id == str(store_a)
    assert rebind.emp_id == 9 and rebind.current_empid == 6 and rebind.has_assignment

    # (e) invalid — 비정수 emp_id (매장은 매칭됨)
    invalid = bob[ACTION_INVALID][0]
    assert invalid.store_id == str(store_b)
    assert invalid.emp_id is None and invalid.emp_id_raw == "ABC"

    # (c) new_assignment — 배정 행 없음
    carol = _entries_by_action(by_email[email3])
    new = carol[ACTION_NEW_ASSIGNMENT][0]
    assert new.store_id == str(store_b)
    assert new.emp_id == 4 and new.current_empid is None and not new.has_assignment


# ---------------------------------------------------------------------------
# (f) match_store — name/code 정규화 매칭 + 모호(동일 키 매장 2개) → None
# ---------------------------------------------------------------------------


def test_match_store_normalization_and_ambiguity() -> None:
    org_id = uuid4()
    # 미저장 Store 는 id 기본값이 안 채워지므로 명시 (모호 판정이 id 유니크화 기반)
    il_fiora = Store(id=uuid4(), organization_id=org_id, name="IL FIORA", code="IFO")
    bbq = Store(id=uuid4(), organization_id=org_id, name="M KOREAN BBQ", code="MKB")
    index = build_store_index([il_fiora, bbq])

    # name 정규화 매칭 — 대소문자/구분자 무시
    assert match_store(index, "il-fiora!!", None) is il_fiora
    # COMPANY 미매칭이면 CORP_ABR_3(code) 폴백
    assert match_store(index, "no such company", "ifo") is il_fiora
    # name == code 로 이중 등록된 자기 자신은 모호 아님 (id 유니크화)
    solo = Store(id=uuid4(), organization_id=org_id, name="SOLO", code="SOLO")
    assert match_store(build_store_index([solo]), "solo", None) is solo

    # 모호 — 정규화 키가 같은 매장 2개 → None
    twin1 = Store(id=uuid4(), organization_id=org_id, name="TWIN", code=None)
    twin2 = Store(id=uuid4(), organization_id=org_id, name="T-W-I-N", code=None)
    assert match_store(build_store_index([twin1, twin2]), "twin", None) is None
    # 전혀 없는 키 → None
    assert match_store(index, "ghost mart", None) is None


# ---------------------------------------------------------------------------
# commit — (g) rebind 반영 + 멱등
# ---------------------------------------------------------------------------


async def test_commit_rebind_applies_then_idempotent_skip(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "A")
    u = await _make_user(db, ctx, "u", f"u.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, u, store, 5)
    await db.commit()

    r1 = await svc.commit(db, ctx.org_id, [(u, store, 7)])
    assert len(r1.applied) == 1 and not r1.skipped and not r1.rejected and not r1.renumbered
    assert r1.applied[0]["empid"] == 7 and r1.applied[0]["created"] is False
    assert (await _empids_in_store(db, store))[u] == 7

    # 같은 요청 재실행 → skipped(already set), DB 불변 (멱등)
    r2 = await svc.commit(db, ctx.org_id, [(u, store, 7)])
    assert not r2.applied and not r2.renumbered
    assert len(r2.skipped) == 1 and r2.skipped[0]["reason"] == "already set"
    assert (await _empids_in_store(db, store))[u] == 7


# ---------------------------------------------------------------------------
# commit — (h) 순열 교환 (A:5→3, B:3→5) 이 IntegrityError 없이 통과
# ---------------------------------------------------------------------------


async def test_commit_swap_passes_partial_unique(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "A")
    ua = await _make_user(db, ctx, "ua", f"ua.{ctx.sfx}@example.com")
    ub = await _make_user(db, ctx, "ub", f"ub.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, ua, store, 5)
    await _give_empid(db, ctx, ub, store, 3)
    await db.commit()

    # 3-phase(비우기→기입) 덕에 (store, empid) partial unique 에 안 걸려야 한다
    r = await svc.commit(db, ctx.org_id, [(ua, store, 3), (ub, store, 5)])
    assert len(r.applied) == 2 and not r.rejected and not r.renumbered

    final = await _empids_in_store(db, store)
    assert final[ua] == 3 and final[ub] == 5


# ---------------------------------------------------------------------------
# commit — (i) 번호 뺏긴 제3자 재채번(renumbered) + 매장 내 unique 유지
# ---------------------------------------------------------------------------


async def test_commit_renumbers_displaced_third_party(db: AsyncSession, ctx: Ctx) -> None:
    store = await _make_store(db, ctx, "A")
    ux = await _make_user(db, ctx, "ux", f"ux.{ctx.sfx}@example.com")
    uy = await _make_user(db, ctx, "uy", f"uy.{ctx.sfx}@example.com")
    uz = await _make_user(db, ctx, "uz", f"uz.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, ux, store, 1)
    await _give_empid(db, ctx, uy, store, 2)
    await _give_empid(db, ctx, uz, store, 3)
    await db.commit()

    # x 가 3 을 가져감 → 기존 3 소유자 z 는 next_empid 로 재채번
    r = await svc.commit(db, ctx.org_id, [(ux, store, 3)])
    assert len(r.applied) == 1 and r.applied[0]["empid"] == 3
    assert len(r.renumbered) == 1
    assert r.renumbered[0]["old"] == 3 and r.renumbered[0]["new"] == 4

    final = await _empids_in_store(db, store)
    assert final[ux] == 3 and final[uy] == 2 and final[uz] == 4
    # 매장 내 unique 유지
    values = [v for v in final.values() if v is not None]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# commit — (j) new_assignment: OrgMemberStore 행 생성 (is_work_assignment=True)
# ---------------------------------------------------------------------------


async def test_commit_new_assignment_creates_member_store_row(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    u = await _make_user(db, ctx, "new", f"new.{ctx.sfx}@example.com")  # 배정 행 없음
    await db.commit()

    r = await svc.commit(db, ctx.org_id, [(u, store, 9)])
    assert len(r.applied) == 1 and not r.rejected
    assert r.applied[0]["empid"] == 9 and r.applied[0]["created"] is True

    row = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == await _member_id(db, ctx, u),
                OrgMemberStore.store_id == store,
            )
        )
    ).scalar_one()
    assert row.empid == 9
    assert row.is_work_assignment is True and row.is_manager is False


# ---------------------------------------------------------------------------
# commit — (k) 같은 매장에 두 사람이 같은 신규 값 요청 → 1 applied + 1 rejected
# ---------------------------------------------------------------------------


async def test_commit_duplicate_value_in_request_rejects_one(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    ua = await _make_user(db, ctx, "da", f"da.{ctx.sfx}@example.com")
    ub = await _make_user(db, ctx, "db", f"db.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, ua, store, 1)
    await _give_empid(db, ctx, ub, store, 2)
    await db.commit()

    # 둘 다 9 를 요청 — 전체 롤백(DuplicateError) 없이 첫 요청만 반영, 나머지는 거절
    r = await svc.commit(db, ctx.org_id, [(ua, store, 9), (ub, store, 9)])
    assert len(r.applied) == 1
    assert len(r.rejected) == 1
    assert r.rejected[0]["reason"] == "duplicate empid for this store in request"
    assert not r.renumbered

    final = await _empids_in_store(db, store)
    assert final[ua] == 9 and final[ub] == 2  # ub 는 기존 값 유지


# ---------------------------------------------------------------------------
# commit — (l) 보유자 우선: 현재 보유자와 타인이 같은 값 요청 시 보유자가 이긴다
# ---------------------------------------------------------------------------


async def test_commit_duplicate_value_holder_wins_regardless_of_order(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    holder = await _make_user(db, ctx, "hold", f"hold.{ctx.sfx}@example.com")
    other = await _make_user(db, ctx, "oth", f"oth.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, holder, store, 3)
    await _give_empid(db, ctx, other, store, 1)
    await db.commit()

    # [타인 먼저, 보유자 나중] 순서로 같은 3 을 요청 — 보유자가 살아남아야 함
    r = await svc.commit(db, ctx.org_id, [(other, store, 3), (holder, store, 3)])
    assert len(r.skipped) == 1 and r.skipped[0]["reason"] == "already set"
    assert len(r.rejected) == 1
    assert r.rejected[0]["user_id"] == str(other)
    assert not r.applied and not r.renumbered

    final = await _empids_in_store(db, store)
    assert final[holder] == 3 and final[other] == 1  # 아무도 안 움직임


# ---------------------------------------------------------------------------
# commit — (m) 그룹 공유 스코프 2-pass: 재채번이 형제 매장 기입값과 충돌하지 않음
# ---------------------------------------------------------------------------


async def test_commit_group_scope_renumber_after_sibling_writes(
    db: AsyncSession, ctx: Ctx
) -> None:
    group = StoreGroup(
        organization_id=ctx.org_id, name=f"__imptest_grp_{ctx.sfx}__",
        numbering_mode="group",
    )
    db.add(group)
    await db.flush()
    store_a = await _make_store(db, ctx, "A", group_id=group.id)
    store_b = await _make_store(db, ctx, "B", group_id=group.id)
    u1 = await _make_user(db, ctx, "g1", f"g1.{ctx.sfx}@example.com")
    u2 = await _make_user(db, ctx, "g2", f"g2.{ctx.sfx}@example.com")
    u3 = await _make_user(db, ctx, "g3", f"g3.{ctx.sfx}@example.com")
    await _give_empid(db, ctx, u1, store_a, 1)
    await _give_empid(db, ctx, u2, store_a, 2)
    await _give_empid(db, ctx, u3, store_b, 3)  # 그룹 공유 max = 3
    await db.commit()

    # A: u2 가 1 을 가져감 → u1 재채번 필요. B: u3 → 4 로 rebind.
    # 1-pass 순차 처리라면 u1 재채번이 4 를 받아 B 의 기입값 4 와 그룹 스코프 중복이 된다.
    # 2-pass 에서는 전 매장 기입(4 포함) 후 재채번하므로 u1 은 5 를 받아야 한다.
    r = await svc.commit(
        db, ctx.org_id, [(u2, store_a, 1), (u3, store_b, 4)]
    )
    assert len(r.applied) == 2 and not r.rejected
    assert len(r.renumbered) == 1
    assert r.renumbered[0]["old"] == 1 and r.renumbered[0]["new"] == 5

    a = await _empids_in_store(db, store_a)
    b = await _empids_in_store(db, store_b)
    assert a[u2] == 1 and a[u1] == 5 and b[u3] == 4
    # 그룹 스코프 전체에서 중복 없음
    all_values = [v for v in list(a.values()) + list(b.values()) if v is not None]
    assert len(all_values) == len(set(all_values))
