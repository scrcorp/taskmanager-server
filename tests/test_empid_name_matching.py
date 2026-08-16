"""EMPID 임포트 — 이름 매칭 티어 · 매장 오버라이드 · 변경 이력.

email/CREWID 가 아예 없는 급여 마스터류 파일(Labor_master) 대응 (2026-08-15):
- 이름 토큰 일치가 **유일**할 때만 자동 매칭 (matched_by="name")
- 동명이인(DB 중복 계정 실존 — 예: 같은 full_name 2계정)은 절대 자동 매칭하지 않음
- 매장 표기 오탈자("MBK")는 store_overrides 로 운영자가 수동 매핑 후 재-preview
- commit / 자동채번은 empid_changes 원장에 old→new + source + channel 을 남긴다
"""
from __future__ import annotations

import io
from typing import AsyncIterator
from uuid import UUID, uuid4

import openpyxl
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.empid_change import EmpidChange
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.services import empid_import_service as svc
from app.services.org_numbering import ensure_member_store

pytestmark = pytest.mark.asyncio


class Ctx:
    def __init__(self, org_id: UUID, role_id: UUID, sfx: str):
        self.org_id = org_id
        self.role_id = role_id
        self.sfx = sfx


@pytest_asyncio.fixture
async def ctx(db: AsyncSession) -> AsyncIterator[Ctx]:
    sfx = uuid4().hex[:8]
    org = Organization(name=f"__nmtest_org_{sfx}__")
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


async def _make_user(
    db: AsyncSession, ctx: Ctx, tag: str, full_name: str, email: str | None = None
) -> UUID:
    user = User(
        organization_id=ctx.org_id, role_id=ctx.role_id,
        username=f"__nmtest_{tag}_{ctx.sfx}", full_name=full_name,
        password_hash="x", email=email, is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(OrgMember(user_id=user.id, organization_id=ctx.org_id, role_id=ctx.role_id))
    await db.flush()
    return user.id


async def _make_store(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    store = Store(
        organization_id=ctx.org_id,
        name=f"NMTEST STORE {tag} {ctx.sfx}", timezone="UTC",
    )
    db.add(store)
    await db.flush()
    return store.id


def _xlsx_no_email(rows: list[list]) -> bytes:
    """급여 마스터 스타일 — email/CREWID 컬럼 없음, WORKNOTE=매장 코드."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["# company", "name", "FIRST_NAME", "LAST_NAME", "emp_id", "WORKNOTE"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 이름 매칭 티어
# ---------------------------------------------------------------------------


async def test_unique_name_auto_matches(db: AsyncSession, ctx: Ctx) -> None:
    """토큰 일치가 유일하면 people 로 자동 매칭 (matched_by='name')."""
    store_id = await _make_store(db, ctx, "A")
    uid = await _make_user(db, ctx, "maria", "Maria Santos")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7,
         f"NMTEST STORE A {ctx.sfx}"],
    ])
    result = await svc.preview(db, ctx.org_id, content, "roster.xlsx")

    assert result.counts()["matched_by_name"] == 1
    person = result.people[0]
    assert person.matched_by == "name"
    assert person.user_id == str(uid)
    assert person.entries[0].action == "new_assignment"
    assert person.entries[0].store_id == str(store_id)


async def test_duplicate_name_defers_with_candidates(db: AsyncSession, ctx: Ctx) -> None:
    """같은 이름 계정이 2개면 자동 매칭 금지 — deferred + 후보 전체 나열."""
    await _make_store(db, ctx, "A")
    u1 = await _make_user(db, ctx, "alec1", "Alec Wong")
    u2 = await _make_user(db, ctx, "alec2", "Alec Wong")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "ALEC WONG (ALEC)", "Alec", "Wong", 9,
         f"NMTEST STORE A {ctx.sfx}"],
    ])
    result = await svc.preview(db, ctx.org_id, content, "roster.xlsx")

    assert result.counts()["matched_by_name"] == 0
    assert len(result.people) == 0
    assert len(result.deferred) == 1
    person = result.deferred[0]
    assert "multiple accounts" in person.note
    ids = {c["user_id"] for c in person.similar_users}
    assert {str(u1), str(u2)} <= ids


async def test_fuzzy_name_suggests_but_never_auto(db: AsyncSession, ctx: Ctx) -> None:
    """철자 흔들림(DEIGO↔Diego-Saavedra)은 제안만 — 자동 등록 금지."""
    await _make_store(db, ctx, "A")
    uid = await _make_user(db, ctx, "anto", "Antonino Diego-Saavedra")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "ANTONIO DEIGO (ANTONIO)", "Antonio", "Deigo", 5,
         f"NMTEST STORE A {ctx.sfx}"],
    ])
    result = await svc.preview(db, ctx.org_id, content, "roster.xlsx")

    assert result.counts()["matched_by_name"] == 0
    assert len(result.deferred) == 1
    person = result.deferred[0]
    assert "close name match" in person.note
    assert person.similar_users[0]["user_id"] == str(uid)


# ---------------------------------------------------------------------------
# 매장 오버라이드
# ---------------------------------------------------------------------------


async def test_store_override_maps_unknown_code(db: AsyncSession, ctx: Ctx) -> None:
    """미매칭 코드("MBK")를 오버라이드로 매장에 매핑하면 재-preview 에서 해소된다."""
    store_id = await _make_store(db, ctx, "A")
    await _make_user(db, ctx, "maria", "Maria Santos")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MBK"],
    ])
    r1 = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    assert r1.counts()["unmatched_store"] == 1
    assert r1.unmatched_stores[0]["key"] == "MBK"

    r2 = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        store_overrides={"MBK": str(store_id)},
    )
    assert r2.counts()["unmatched_store"] == 0
    assert r2.people[0].entries[0].store_id == str(store_id)


async def test_store_override_ignores_foreign_store(db: AsyncSession, ctx: Ctx) -> None:
    """타 org 매장 id 는 조용히 무시 — unmatched 유지 (경계 방어)."""
    await _make_user(db, ctx, "maria", "Maria Santos")
    other_org = Organization(name=f"__nmtest_other_{ctx.sfx}__")
    db.add(other_org)
    await db.flush()
    foreign = Store(organization_id=other_org.id, name="FOREIGN", timezone="UTC")
    db.add(foreign)
    await db.flush()
    foreign_id = foreign.id
    await db.commit()
    try:
        content = _xlsx_no_email([
            ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MBK"],
        ])
        r = await svc.preview(
            db, ctx.org_id, content, "roster.xlsx",
            store_overrides={"MBK": str(foreign_id)},
        )
        assert r.counts()["unmatched_store"] == 1
    finally:
        async with async_session() as s:
            await s.execute(delete(Organization).where(Organization.id == other_org.id))
            await s.commit()


async def test_company_override_does_not_swallow_sibling_code(
    db: AsyncSession, ctx: Ctx
) -> None:
    """법인 키 오버라이드가 이미 코드로 매칭되는 형제 매장 행을 삼키지 않는다.

    corp_abr(매장 코드)를 company 보다 먼저 보기 때문 — M KOREAN BBQ(MKB+MSK)에서
    MKB 만 오버라이드해도 MSK 행은 원래 매장으로 남아야 한다.
    """
    store_a = await _make_store(db, ctx, "A")  # MKB 대응 (오버라이드로)
    store_b = await _make_store(db, ctx, "B")  # 코드 매칭 대상
    # store B 에 코드 부여
    b = await db.get(Store, store_b)
    b.code = "MSK"
    await db.commit()

    await _make_user(db, ctx, "maria", "Maria Santos")
    await _make_user(db, ctx, "juan", "Juan Perez")
    await db.commit()

    content = _xlsx_no_email([
        ["M KOREAN BBQ", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MKB"],
        ["M KOREAN BBQ", "JUAN PEREZ (JUAN)", "Juan", "Perez", 8, "MSK"],
    ])
    r = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        # company 정규화 키로 오버라이드 — MSK 행이 삼켜지면 안 된다
        store_overrides={"MKOREANBBQ": str(store_a)},
    )
    by_name = {p.name: p for p in r.people}
    assert by_name["MARIA SANTOS (MARIA)"].entries[0].store_id == str(store_a)
    assert by_name["JUAN PEREZ (JUAN)"].entries[0].store_id == str(store_b)


# ---------------------------------------------------------------------------
# empid 변경 이력
# ---------------------------------------------------------------------------


async def _changes(org_id: UUID) -> list[EmpidChange]:
    async with async_session() as s:
        return list((
            await s.execute(
                select(EmpidChange).where(EmpidChange.organization_id == org_id)
                .order_by(EmpidChange.created_at)
            )
        ).scalars().all())


async def test_commit_writes_change_ledger(db: AsyncSession, ctx: Ctx) -> None:
    """commit 의 기입/재채번이 empid_changes 에 남는다 (actor 포함)."""
    store_id = await _make_store(db, ctx, "A")
    uid = await _make_user(db, ctx, "maria", "Maria Santos")
    actor = await _make_user(db, ctx, "admin", "Admin Person")
    await db.commit()

    result = await svc.commit(
        db, ctx.org_id, [(uid, store_id, 42)], actor_id=actor,
    )
    assert len(result.applied) == 1

    changes = await _changes(ctx.org_id)
    assert len(changes) == 1
    ch = changes[0]
    assert ch.user_id == uid
    assert ch.person_name == "Maria Santos"
    assert ch.old_empid is None and ch.new_empid == 42
    assert ch.source == "commit"
    assert ch.changed_by == actor
    # 요청 컨텍스트 밖(테스트 직접 호출) → system
    assert ch.channel == "system"


async def test_auto_assign_writes_change_ledger(db: AsyncSession, ctx: Ctx) -> None:
    """매장 배정 자동 채번(ensure_member_store)도 원장에 남는다 (source=auto)."""
    store_id = await _make_store(db, ctx, "A")
    uid = await _make_user(db, ctx, "maria", "Maria Santos")
    await db.commit()

    await ensure_member_store(db, uid, store_id)
    await db.commit()

    changes = await _changes(ctx.org_id)
    assert len(changes) == 1
    assert changes[0].source == "auto"
    assert changes[0].new_empid is not None
    assert changes[0].changed_by is None


async def test_group_override_resolves_single_store_group(
    db: AsyncSession, ctx: Ctx
) -> None:
    """그룹 매핑 — 단일 매장 그룹은 그 매장으로 축약, 다매장 그룹은 정식 스코프.

    다매장 그룹 스코프에서 배정 없는 사람은 needs_store (그룹 매장 중 운영자 선택).
    행의 매장은 파일이 아니라 사람의 그룹 내 배정이 결정한다는 원칙의 경계 케이스.
    """
    from app.models.organization import StoreGroup

    await _make_user(db, ctx, "maria", "Maria Santos")

    single = StoreGroup(organization_id=ctx.org_id, name=f"SINGLE {ctx.sfx}")
    multi = StoreGroup(organization_id=ctx.org_id, name=f"MULTI {ctx.sfx}")
    db.add_all([single, multi])
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    (await db.get(Store, store_a)).group_id = single.id
    store_b = await _make_store(db, ctx, "B")
    store_c = await _make_store(db, ctx, "C")
    (await db.get(Store, store_b)).group_id = multi.id
    (await db.get(Store, store_c)).group_id = multi.id
    single_id, multi_id = single.id, multi.id
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MBK"],
    ])
    # 단일 매장 그룹 → 확정
    r1 = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        store_overrides={"MBK": str(single_id)},
    )
    assert r1.counts()["unmatched_store"] == 0
    assert r1.people[0].entries[0].store_id == str(store_a)

    # 다매장 그룹 → 무시 (여전히 unmatched — 콘솔이 비활성 옵션으로 안내)
    # 키를 달리한다 — 위 매핑이 별칭으로 저장돼 MBK 는 이제 자동 매칭되기 때문.
    content2 = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "XXX"],
    ])
    r2 = await svc.preview(
        db, ctx.org_id, content2, "roster.xlsx",
        store_overrides={"XXX": str(multi_id)},
    )
    # 다매장 그룹은 이제 정식 스코프 — 미배정자는 needs_store 로 매장 선택 대기
    assert r2.counts()["unmatched_store"] == 0
    assert r2.counts()["needs_store"] == 1
    entry = r2.people[0].entries[0]
    assert entry.action == "needs_store"
    assert entry.group_id == str(multi_id)
    assert len(entry.group_stores or []) == 2


# ---------------------------------------------------------------------------
# 별칭 영구화 — 한 번 매핑하면 다음 업로드부터 자동
# ---------------------------------------------------------------------------


async def test_override_persists_as_alias_and_auto_applies(
    db: AsyncSession, ctx: Ctx
) -> None:
    """당회 매핑이 org 별칭으로 저장되고, 다음 preview(매핑 없음)에 자동 적용된다."""
    store_id = await _make_store(db, ctx, "A")
    actor = await _make_user(db, ctx, "admin", "Admin Person")
    await _make_user(db, ctx, "maria", "Maria Santos")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MBK"],
    ])
    # 1차: 매핑과 함께 → 별칭 저장
    r1 = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        store_overrides={"MBK": str(store_id)}, actor_id=actor,
    )
    assert r1.counts()["unmatched_store"] == 0

    # 2차: 매핑 없이 새 preview — 저장된 별칭이 자동 적용
    r2 = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    assert r2.counts()["unmatched_store"] == 0
    assert r2.people[0].entries[0].store_id == str(store_id)
    assert [a["key"] for a in r2.saved_aliases] == ["MBK"]


async def test_explicit_override_beats_saved_alias(db: AsyncSession, ctx: Ctx) -> None:
    """당회 명시 매핑 > 저장된 별칭 — 틀린 학습은 다시 골라 덮어쓴다(upsert)."""
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    await _make_user(db, ctx, "maria", "Maria Santos")
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "MBK"],
    ])
    await svc.preview(
        db, ctx.org_id, content, "roster.xlsx", store_overrides={"MBK": str(store_a)},
    )
    # 잘못 배웠다고 치고 B 로 재매핑
    r = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx", store_overrides={"MBK": str(store_b)},
    )
    assert r.people[0].entries[0].store_id == str(store_b)
    # upsert 확인 — 이후 자동 적용도 B
    r2 = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    assert r2.people[0].entries[0].store_id == str(store_b)


async def test_saved_group_alias_follows_group_membership(
    db: AsyncSession, ctx: Ctx
) -> None:
    """그룹 대상 별칭은 그룹 id 로 저장 — 매장 교체 후에도 별칭이 그룹을 따라간다."""
    from app.models.organization import StoreGroup

    await _make_user(db, ctx, "maria", "Maria Santos")
    grp = StoreGroup(organization_id=ctx.org_id, name=f"G {ctx.sfx}")
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    (await db.get(Store, store_a)).group_id = grp.id
    grp_id = grp.id
    await db.commit()

    content = _xlsx_no_email([
        ["ACME CORP", "MARIA SANTOS (MARIA)", "Maria", "Santos", 7, "ODG"],
    ])
    await svc.preview(
        db, ctx.org_id, content, "roster.xlsx", store_overrides={"ODG": str(grp_id)},
    )
    # 그룹의 매장을 A→B 로 교체
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = None
    (await db.get(Store, store_b)).group_id = grp_id
    await db.commit()

    r = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    assert r.people[0].entries[0].store_id == str(store_b)


# ---------------------------------------------------------------------------
# 그룹 스코프 — 사람의 배정이 매장을 결정 + 양측 대조
# ---------------------------------------------------------------------------


async def test_group_scope_resolves_store_from_assignment(
    db: AsyncSession, ctx: Ctx
) -> None:
    """다매장 그룹 매핑에서 행의 매장은 그 사람의 그룹 내 배정이 정한다.

    겸업자(그룹 내 2개 매장 배정, 번호 상이)는 전 배정에 같은 번호로 통일 rebind —
    numbering_mode=group 의 1인 1번호 의미 (실제 KYLE 5015/5028 케이스).
    """
    from app.models.organization import StoreGroup

    grp = StoreGroup(organization_id=ctx.org_id, name=f"ODG {ctx.sfx}")
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = grp.id
    (await db.get(Store, store_b)).group_id = grp.id
    gloria = await _make_user(db, ctx, "gloria", "Gloria Cano")
    kyle = await _make_user(db, ctx, "kyle", "Kyle Nguyen")
    grp_id = grp.id
    await db.flush()
    # Gloria 는 B 에만 배정(#14), Kyle 은 A(#5015)·B(#5028) 겸업
    for uid, sid, emp in [(gloria, store_b, 14), (kyle, store_a, 5015), (kyle, store_b, 5028)]:
        db.add(OrgMemberStore(
            org_member_id=(
                await db.execute(select(OrgMember.id).where(OrgMember.user_id == uid))
            ).scalar_one(),
            store_id=sid, empid=emp,
        ))
    await db.commit()

    content = _xlsx_no_email([
        ["M KOREAN BBQ", "GLORIA CANO (GLORIA)", "Gloria", "Cano", 1014, None],
        ["M KOREAN BBQ", "KYLE NGUYEN (KYLE)", "Kyle", "Nguyen", 5015, None],
    ])
    r = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        store_overrides={"MKOREANBBQ": str(grp_id)},
    )
    by_name = {p.user_full_name: p for p in r.people}

    # Gloria — 배정 매장(B) 자동 결정, 14→1014 rebind
    g = by_name["Gloria Cano"].entries
    assert len(g) == 1 and g[0].store_id == str(store_b)
    assert g[0].action == "rebind" and g[0].current_empid == 14

    # Kyle — 겸업 2배정 모두 엔트리: A 는 same(5015), B 는 rebind(5028→5015, 통일 경고)
    k = {e.store_id: e for e in by_name["Kyle Nguyen"].entries}
    assert k[str(store_a)].action == "same"
    assert k[str(store_b)].action == "rebind"
    assert "unifying" in (k[str(store_b)].warning or "")


async def test_reconciliation_two_sided_diff(db: AsyncSession, ctx: Ctx) -> None:
    """양측 대조 — **사람 단위** 3분류가 그룹 단위로 나온다.

    번호 단위 비교는 rebind 대기자가 양쪽에 모순처럼 찍히므로 사람 기준:
    matched(전이 요약) / htm_unmatched(번호 지정 대상) / file_unmatched.
    멤버 매장 코드로 직접 매칭된 행(MSK 등)도 그룹에 접힌다.
    """
    from app.models.organization import StoreGroup

    grp = StoreGroup(organization_id=ctx.org_id, name=f"ODG {ctx.sfx}")
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = grp.id
    (await db.get(Store, store_b)).group_id = grp.id
    (await db.get(Store, store_b)).code = "MSK"
    gloria = await _make_user(db, ctx, "gloria", "Gloria Cano")
    hank = await _make_user(db, ctx, "hank", "Hank Only In Htm")
    grp_id = grp.id
    await db.flush()
    for uid, sid, emp in [(gloria, store_b, 1014), (hank, store_a, 77)]:
        db.add(OrgMemberStore(
            org_member_id=(
                await db.execute(select(OrgMember.id).where(OrgMember.user_id == uid))
            ).scalar_one(),
            store_id=sid, empid=emp,
        ))
    await db.commit()

    content = _xlsx_no_email([
        # MSK 코드로 직접 매칭되는 행 — 그룹 diff 에 접혀야 함
        ["M KOREAN BBQ", "GLORIA CANO (GLORIA)", "Gloria", "Cano", 1014, "MSK"],
        # 파일에만 있는 번호 (HTM 미존재 인물)
        ["M KOREAN BBQ", "NANCY GUTIERRES (NANCY)", "Nancy", "Gutierres", 1044, None],
    ])
    r = await svc.preview(
        db, ctx.org_id, content, "roster.xlsx",
        store_overrides={"MKOREANBBQ": str(grp_id)},
    )
    recon = [x for x in r.reconciliation if x["scope"] == "group"]
    assert len(recon) == 1
    rec = recon[0]
    # 매칭된 사람 — Gloria (same 1014 도 matched 에 요약된다)
    assert [m["name"] for m in rec["matched"]] == ["Gloria Cano"]
    assert rec["matched"][0]["changes"][0]["new"] == 1014
    # HTM 미매칭 — Hank(#77), 번호 지정 대상 (user_id·store 포함)
    assert [x["name"] for x in rec["htm_unmatched"]] == ["Hank Only In Htm"]
    assert rec["htm_unmatched"][0]["current_empid"] == 77
    assert rec["htm_unmatched"][0]["user_id"]
    # 파일 미매칭 — Nancy(#1044)
    assert [x["empid"] for x in rec["file_unmatched"]] == [1044]
    # 매장 항목으로 중복 방출되지 않음 (그룹에 접힘)
    assert not [x for x in r.reconciliation if x["scope"] == "store"]
    assert r.counts()["htm_unmatched"] == 1 and r.counts()["file_unmatched"] == 1


# ---------------------------------------------------------------------------
# 매장→그룹 스코프 승격 (shared numbering)
# ---------------------------------------------------------------------------


async def test_store_code_row_promotes_to_shared_group(
    db: AsyncSession, ctx: Ctx
) -> None:
    """corp 가 매장 코드(MSK)인 행도 shared 그룹이면 그룹 통일을 탄다.

    실제 사고(2026-08-16 Jiho): corp=MSK 행이 매장 스코프로 좁혀져 MSK 만
    4→6006 이 되고 같은 그룹 MBQ 의 옛 번호가 남았다. 승격 후에는 그 사람의
    그룹 내 전 배정에 같은 번호가 간다.
    """
    from app.models.organization import StoreGroup

    grp = StoreGroup(organization_id=ctx.org_id, name=f"ODG {ctx.sfx}")  # mode 기본=group
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = grp.id
    (await db.get(Store, store_b)).group_id = grp.id
    (await db.get(Store, store_b)).code = f"MSK{ctx.sfx[:4]}"
    code_b = f"MSK{ctx.sfx[:4]}"
    jiho = await _make_user(db, ctx, "jiho", "Jiho Yoon")
    await db.flush()
    for sid, emp in [(store_a, 6), (store_b, 4)]:
        db.add(OrgMemberStore(
            org_member_id=(
                await db.execute(select(OrgMember.id).where(OrgMember.user_id == jiho))
            ).scalar_one(),
            store_id=sid, empid=emp,
        ))
    await db.commit()

    content = _xlsx_no_email([
        # corp 만 매장 B 코드 — company 는 org 에 없는 이름 (매장 키가 유일 해석 경로)
        ["M KOREAN BBQ", "JIHO YOON (JIHO)", "Jiho", "Yoon", 6006, code_b],
    ])
    r = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    person = {p.user_full_name: p for p in r.people}["Jiho Yoon"]
    ents = {e.store_id: e for e in person.entries}
    # 승격 — 매장 B 하나가 아니라 그룹 내 두 배정 모두 rebind 로 통일
    assert set(ents) == {str(store_a), str(store_b)}
    assert ents[str(store_a)].action == "rebind" and ents[str(store_a)].current_empid == 6
    assert ents[str(store_b)].action == "rebind" and ents[str(store_b)].current_empid == 4
    assert all(e.emp_id == 6006 for e in ents.values())
    assert "unifying" in (ents[str(store_a)].warning or "")


async def test_store_code_row_stays_store_when_mode_store(
    db: AsyncSession, ctx: Ctx
) -> None:
    """numbering_mode=store 그룹은 승격하지 않는다 — 매장별 독립 번호가 정책."""
    from app.models.organization import NUMBERING_MODE_STORE, StoreGroup

    grp = StoreGroup(
        organization_id=ctx.org_id, name=f"IND {ctx.sfx}",
        numbering_mode=NUMBERING_MODE_STORE,
    )
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = grp.id
    (await db.get(Store, store_b)).group_id = grp.id
    (await db.get(Store, store_b)).code = f"IND{ctx.sfx[:4]}"
    code_b = f"IND{ctx.sfx[:4]}"
    kim = await _make_user(db, ctx, "kim", "Kim Indep")
    await db.flush()
    for sid, emp in [(store_a, 6), (store_b, 4)]:
        db.add(OrgMemberStore(
            org_member_id=(
                await db.execute(select(OrgMember.id).where(OrgMember.user_id == kim))
            ).scalar_one(),
            store_id=sid, empid=emp,
        ))
    await db.commit()

    content = _xlsx_no_email([
        ["WHATEVER CORP", "KIM INDEP (KIM)", "Kim", "Indep", 6006, code_b],
    ])
    r = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    person = {p.user_full_name: p for p in r.people}["Kim Indep"]
    # 매장 B 스코프 유지 — A 의 6 은 건드리지 않는다
    assert [e.store_id for e in person.entries] == [str(store_b)]
    assert person.entries[0].action == "rebind" and person.entries[0].current_empid == 4


async def test_promoted_row_needs_store_carries_hint(
    db: AsyncSession, ctx: Ctx
) -> None:
    """승격 행의 needs_store 는 corp 가 지목했던 매장을 hint 로 내려준다."""
    from app.models.organization import StoreGroup

    grp = StoreGroup(organization_id=ctx.org_id, name=f"ODG {ctx.sfx}")
    db.add(grp)
    await db.flush()
    store_a = await _make_store(db, ctx, "A")
    store_b = await _make_store(db, ctx, "B")
    (await db.get(Store, store_a)).group_id = grp.id
    (await db.get(Store, store_b)).group_id = grp.id
    (await db.get(Store, store_b)).code = f"MSK{ctx.sfx[:4]}"
    code_b = f"MSK{ctx.sfx[:4]}"
    # 그룹 내 어느 매장에도 배정이 없는 사람
    await _make_user(db, ctx, "solo", "Solo Nowhere")
    await db.commit()

    content = _xlsx_no_email([
        ["M KOREAN BBQ", "SOLO NOWHERE (SOLO)", "Solo", "Nowhere", 1139, code_b],
    ])
    r = await svc.preview(db, ctx.org_id, content, "roster.xlsx")
    person = {p.user_full_name: p for p in r.people}["Solo Nowhere"]
    e = person.entries[0]
    assert e.action == "needs_store"
    assert e.group_id and e.hint_store_id == str(store_b)
