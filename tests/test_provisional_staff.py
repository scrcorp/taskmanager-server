"""미가입(Provisional) 직원 계정 — fail-closed 계약 테스트.

핵심 불변식: 유령은 is_active=False + is_provisional=True 이므로
로그인·PIN·알림/팁/리포트 대상 쿼리에서 **자동 제외**되고,
스케줄 후보처럼 명시적으로 include_provisional 을 준 곳에서만 보인다.

각 테스트는 uuid4 suffix 로 고유 org 를 만들어 격리한다(org 삭제 CASCADE 로 정리).
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
from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.repositories.user_repository import user_repository
from app.services import provisional_staff_service as prov_svc
from app.utils.password import verify_password

pytestmark = pytest.mark.asyncio


class Ctx:
    """테스트 1개 전용 org 컨텍스트."""

    def __init__(self, org_id: UUID, role_id: UUID, sfx: str):
        self.org_id = org_id
        self.role_id = role_id
        self.sfx = sfx


@pytest_asyncio.fixture
async def ctx(db: AsyncSession) -> AsyncIterator[Ctx]:
    sfx = uuid4().hex[:8]
    org = Organization(name=f"__provtest_org_{sfx}__")
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


async def _make_store(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    store = Store(
        organization_id=ctx.org_id, name=f"PROVTEST {tag} {ctx.sfx}", timezone="UTC"
    )
    db.add(store)
    await db.flush()
    return store.id


async def _make_real_user(db: AsyncSession, ctx: Ctx, tag: str) -> UUID:
    """비교용 정상(가입) 유저."""
    from app.utils.password import hash_password

    user = User(
        organization_id=ctx.org_id,
        role_id=ctx.role_id,
        username=f"__provtest_{tag}_{ctx.sfx}",
        full_name=f"Real {tag}",
        password_hash=hash_password("pw"),
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(OrgMember(user_id=user.id, organization_id=ctx.org_id, role_id=ctx.role_id))
    await db.flush()
    return user.id


# ---------------------------------------------------------------------------
# ① 생성 — fail-closed 속성
# ---------------------------------------------------------------------------


async def test_provisional_is_inactive_no_pin_with_claim_code(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    await db.commit()

    user = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost One", role_id=ctx.role_id, store_ids=[store]
    )

    # fail-closed 3종
    assert user.is_provisional is True
    assert user.is_active is False, "유령은 반드시 비활성 — 로그인/PIN/알림 자동 차단"
    assert user.claim_code and len(user.claim_code) == prov_svc.CLAIM_CODE_LENGTH
    assert user.email is None
    assert user.username.startswith("prov_")

    # PIN 미발급 — 기기 출근 조회의 lookup 키가 없다
    member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.user_id == user.id, OrgMember.organization_id == ctx.org_id
            )
        )
    ).scalar_one()
    assert member.clockin_pin is None
    assert member.crewid is not None, "crewid 는 정상 부여"

    # 비밀번호는 아무도 모르는 값 — 해시 형식은 정상이라 verify_password 가 예외 없이 False
    assert verify_password("", user.password_hash) is False
    assert verify_password("password", user.password_hash) is False

    # 매장 배정 + empid 부여
    ms = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member.id,
                OrgMemberStore.store_id == store,
            )
        )
    ).scalar_one()
    assert ms.empid == 1, "유령도 매장 채번 정책을 그대로 탄다"
    assert ms.is_work_assignment is True


# ---------------------------------------------------------------------------
# ② 조회 — 기본 제외, 명시 개방 시에만 포함
# ---------------------------------------------------------------------------


async def test_active_filter_excludes_provisional_unless_opened(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    real_id = await _make_real_user(db, ctx, "r1")
    from app.models.user_store import UserStore

    db.add(UserStore(user_id=real_id, store_id=store, is_work_assignment=True))
    await db.commit()

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Two", role_id=ctx.role_id, store_ids=[store]
    )

    base = {"store_ids": [store], "is_active": True}

    # 기본: 알림/팁/리포트가 쓰는 방식(is_active=True)이면 유령은 안 보인다
    ids = {u.id for u in await user_repository.get_by_org(db, ctx.org_id, base)}
    assert real_id in ids
    assert ghost.id not in ids, "명시 개방 없이는 유령이 노출되면 안 됨 (fail-closed)"

    # 스케줄 후보처럼 명시 개방하면 포함
    ids_open = {
        u.id
        for u in await user_repository.get_by_org(
            db, ctx.org_id, {**base, "include_provisional": True}
        )
    }
    assert {real_id, ghost.id} <= ids_open

    # 유령만 조회 (콘솔 '미가입' 필터)
    only = await user_repository.get_by_org(
        db, ctx.org_id, {"store_ids": [store], "provisional_only": True}
    )
    assert [u.id for u in only] == [ghost.id]


async def test_schedule_roster_includes_provisional(db: AsyncSession, ctx: Ctx) -> None:
    """스케줄 로스터 필터가 유령을 포함하도록 열려 있는지 (schedule_service 계약)."""
    store = await _make_store(db, ctx, "A")
    await db.commit()
    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Roster", role_id=ctx.role_id, store_ids=[store]
    )

    from app.services.user_service import user_service

    # schedule_service.get_roster 가 쓰는 것과 동일한 필터
    users = await user_service.list_users(
        db,
        ctx.org_id,
        {"store_ids": [store], "is_active": True, "include_provisional": True},
    )
    assert str(ghost.id) in {u.id for u in users}
    # 응답 스키마에 표식이 실려야 콘솔이 뱃지를 그릴 수 있다
    row = next(u for u in users if u.id == str(ghost.id))
    assert row.is_provisional is True and row.is_active is False


# ---------------------------------------------------------------------------
# ③ 로그인 차단 (fail-closed 실증)
# ---------------------------------------------------------------------------


async def test_provisional_cannot_login(db: AsyncSession, ctx: Ctx) -> None:
    from app.services.auth_service import auth_service
    from app.utils.exceptions import UnauthorizedError

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Login", role_id=ctx.role_id
    )
    # 설령 비밀번호를 알아냈다고 가정해도(=해시를 아는 값으로 바꿔도) is_active=False 라 차단
    from app.utils.password import hash_password

    ghost.password_hash = hash_password("known-secret")
    await db.commit()

    from app.schemas.auth import LoginRequest

    creds = LoginRequest(username=ghost.username, password="known-secret")
    for login in (auth_service.admin_login, auth_service.app_login):
        with pytest.raises(UnauthorizedError):
            await login(db, creds, ctx.org_id)


# ---------------------------------------------------------------------------
# ④ 다건 생성 + 인수 코드 재발급
# ---------------------------------------------------------------------------


async def test_bulk_create_and_regenerate_claim_code(
    db: AsyncSession, ctx: Ctx
) -> None:
    store = await _make_store(db, ctx, "A")
    await db.commit()

    users = await prov_svc.create_provisional_users_bulk(
        db,
        ctx.org_id,
        [
            {"full_name": "Bulk One", "role_id": ctx.role_id, "store_ids": [store]},
            {"full_name": "Bulk Two", "role_id": ctx.role_id, "store_ids": [store]},
        ],
    )
    assert len(users) == 2
    codes = {u.claim_code for u in users}
    assert len(codes) == 2, "인수 코드는 org 안에서 서로 달라야 한다"
    # 같은 매장 → empid 는 연번
    empids = sorted(
        (
            await db.execute(
                select(OrgMemberStore.empid).where(OrgMemberStore.store_id == store)
            )
        ).scalars().all()
    )
    assert empids == [1, 2]

    old = users[0].claim_code
    new = await prov_svc.regenerate_claim_code(db, ctx.org_id, users[0].id)
    assert new != old
    await db.refresh(users[0])
    assert users[0].claim_code == new

    # 정상 계정에는 인수 코드가 없다
    from app.utils.exceptions import BadRequestError

    real_id = await _make_real_user(db, ctx, "r2")
    await db.commit()
    with pytest.raises(BadRequestError):
        await prov_svc.regenerate_claim_code(db, ctx.org_id, real_id)


# ---------------------------------------------------------------------------
# ⑤ 인수(claim) — 병합 없이 그 행을 이어받고 empid·스케줄·배정이 따라온다
# ---------------------------------------------------------------------------


async def _issue_verification_token(db: AsyncSession, email: str) -> str:
    """가입에 필요한 이메일 인증 토큰을 테스트용으로 직접 발급."""
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    from app.models.email_verification import EmailVerificationCode

    token = _uuid.uuid4()
    now = datetime.now(timezone.utc)
    db.add(
        EmailVerificationCode(
            email=email.strip().lower(),
            code="000000",
            purpose="registration",
            expires_at=now + timedelta(minutes=10),
            is_used=True,
            verification_token=token,
        )
    )
    await db.commit()
    return str(token)


async def test_claim_takes_over_row_keeping_empid_and_schedule(
    db: AsyncSession, ctx: Ctx
) -> None:
    from app.models.schedule import Schedule
    from app.schemas.auth import RegisterRequest
    from app.services.auth_service import auth_service

    store = await _make_store(db, ctx, "A")
    # staff role priority 40 이어야 가입 경로가 찾는다 (ctx 의 role 이 이미 40)
    await db.commit()

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Claim", role_id=ctx.role_id, store_ids=[store]
    )
    ghost_id, code = ghost.id, ghost.claim_code

    member = (
        await db.execute(
            select(OrgMember).where(OrgMember.user_id == ghost_id)
        )
    ).scalar_one()
    ms = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member.id,
                OrgMemberStore.store_id == store,
            )
        )
    ).scalar_one()
    empid_before, crewid_before = ms.empid, member.crewid

    # 관리자가 유령에게 스케줄을 미리 배정해 둔 상태
    from datetime import date, datetime

    db.add(
        Schedule(
            organization_id=ctx.org_id,
            store_id=store,
            user_id=ghost_id,
            operating_day=date(2026, 8, 10),
            start_at=datetime(2026, 8, 10, 9, 0),
            end_at=datetime(2026, 8, 10, 17, 0),
        )
    )
    await db.commit()

    email = f"claim.{ctx.sfx}@example.com"
    token = await _issue_verification_token(db, email)
    result = await auth_service.app_register(
        db,
        RegisterRequest(
            username=f"claimed_{ctx.sfx}",
            password="Str0ngPass!",
            full_name="Real Person",
            email=email,
            verification_token=token,
            store_ids=[],
            claim_code=code,
        ),
        ctx.org_id,
    )
    assert result.access_token

    # 새 계정이 생기지 않고 그 행이 활성화됐다
    users = (
        await db.execute(select(User).where(User.organization_id == ctx.org_id))
    ).scalars().all()
    assert len(users) == 1, "인수는 새 행을 만들지 않는다 (병합 불필요)"
    claimed = users[0]
    assert claimed.id == ghost_id
    assert claimed.is_provisional is False and claimed.is_active is True
    assert claimed.claim_code is None, "코드는 반납된다"
    assert claimed.username == f"claimed_{ctx.sfx}"
    assert claimed.email == email and claimed.email_verified is True
    assert claimed.clockin_pin is not None, "인수 후에는 PIN 발급 (기기 출근 가능)"

    # empid / crewid / 스케줄이 그대로 따라온다
    await db.refresh(ms)
    await db.refresh(member)
    assert ms.empid == empid_before and member.crewid == crewid_before
    sched_owner = (
        await db.execute(select(Schedule.user_id).where(Schedule.store_id == store))
    ).scalar_one()
    assert sched_owner == ghost_id

    # 이제 로그인 가능
    from app.schemas.auth import LoginRequest

    tokens = await auth_service.app_login(
        db, LoginRequest(username=claimed.username, password="Str0ngPass!"), ctx.org_id
    )
    assert tokens.access_token


async def test_claim_code_is_single_use_and_case_insensitive(
    db: AsyncSession, ctx: Ctx
) -> None:
    from app.services.auth_service import auth_service

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Once", role_id=ctx.role_id
    )
    code = ghost.claim_code

    # 소문자로 입력해도 찾는다
    found = await auth_service.find_provisional_by_claim_code(
        db, ctx.org_id, code.lower()
    )
    assert found is not None and found.id == ghost.id

    # 인수 완료(코드 반납)를 흉내내면 더는 못 찾는다
    ghost.claim_code = None
    ghost.is_provisional = False
    await db.commit()
    assert await auth_service.find_provisional_by_claim_code(db, ctx.org_id, code) is None


# ---------------------------------------------------------------------------
# ⑥ 흡수(absorb) — 코드를 안 쓰고 따로 가입해버린 경우의 폴백
# ---------------------------------------------------------------------------


async def test_absorb_moves_assignments_and_retires_ghost(
    db: AsyncSession, ctx: Ctx
) -> None:
    """유령의 매장·empid·스케줄이 실제 계정으로 옮겨지고 유령 행은 폐기된다."""
    from datetime import date, datetime

    from app.models.schedule import Schedule
    from app.services import provisional_absorb_service as absorb_svc

    store = await _make_store(db, ctx, "A")
    real_id = await _make_real_user(db, ctx, "dup")  # 따로 가입해버린 진짜 계정
    await db.commit()

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Absorb", role_id=ctx.role_id, store_ids=[store]
    )
    g_member = (
        await db.execute(select(OrgMember).where(OrgMember.user_id == ghost.id))
    ).scalar_one()
    ms = (
        await db.execute(
            select(OrgMemberStore).where(OrgMemberStore.org_member_id == g_member.id)
        )
    ).scalar_one()
    empid_before, crewid_before = ms.empid, g_member.crewid

    db.add(
        Schedule(
            organization_id=ctx.org_id,
            store_id=store,
            user_id=ghost.id,
            operating_day=date(2026, 8, 11),
            start_at=datetime(2026, 8, 11, 9, 0),
            end_at=datetime(2026, 8, 11, 17, 0),
        )
    )
    await db.commit()

    # preview 는 DB 를 바꾸지 않는다
    plan = await absorb_svc.preview_absorb(db, ctx.org_id, ghost.id, real_id)
    assert plan.provisional_name == "Ghost Absorb"
    assert plan.moves.get("schedules") == 1
    assert any(t["action"] == "move" for t in plan.store_transfers)
    await db.refresh(ghost)
    assert ghost.is_provisional is True, "preview 는 변경 없음"

    await absorb_svc.absorb(db, ctx.org_id, ghost.id, real_id)

    # 스케줄이 실제 계정으로 이동
    sched_owner = (
        await db.execute(select(Schedule.user_id).where(Schedule.store_id == store))
    ).scalar_one()
    assert sched_owner == real_id

    # 매장 배정 + empid 가 실제 계정의 org_member 로 승계
    t_member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.user_id == real_id, OrgMember.organization_id == ctx.org_id
            )
        )
    ).scalar_one()
    moved = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == t_member.id,
                OrgMemberStore.store_id == store,
            )
        )
    ).scalar_one()
    assert moved.empid == empid_before, "empid 그대로 승계"
    assert t_member.crewid == crewid_before, "대상에 crewid 없었으므로 승계"

    # 유령은 폐기 — 로그인 불가 상태로 소프트 삭제되고 username 을 비켜준다
    await db.refresh(ghost)
    assert ghost.is_active is False and ghost.is_provisional is False
    assert ghost.deleted_at is not None
    assert ghost.username.startswith("absorbed_")
    assert ghost.claim_code is None


async def test_absorb_keeps_target_number_on_store_conflict(
    db: AsyncSession, ctx: Ctx
) -> None:
    """같은 매장에 둘 다 배정돼 있으면 대상 번호를 유지하고 유령 번호는 버린다."""
    from app.models.user_store import UserStore
    from app.services import provisional_absorb_service as absorb_svc
    from app.services.org_numbering import ensure_member_store

    store = await _make_store(db, ctx, "A")
    real_id = await _make_real_user(db, ctx, "conf")
    db.add(UserStore(user_id=real_id, store_id=store, is_work_assignment=True))
    await db.flush()
    await ensure_member_store(db, real_id, store)
    await db.commit()

    t_member = (
        await db.execute(select(OrgMember).where(OrgMember.user_id == real_id))
    ).scalar_one()
    t_ms = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == t_member.id,
                OrgMemberStore.store_id == store,
            )
        )
    ).scalar_one()
    target_empid = t_ms.empid

    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Conflict", role_id=ctx.role_id, store_ids=[store]
    )

    plan = await absorb_svc.preview_absorb(db, ctx.org_id, ghost.id, real_id)
    assert any(t["action"] == "keep_target" for t in plan.store_transfers)
    assert plan.conflicts, "충돌은 미리 알려줘야 한다"

    await absorb_svc.absorb(db, ctx.org_id, ghost.id, real_id)

    rows = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == t_member.id,
                OrgMemberStore.store_id == store,
            )
        )
    ).scalars().all()
    assert len(rows) == 1, "매장당 한 행만 남는다 (uq_org_member_store)"
    assert rows[0].empid == target_empid, "대상 번호 유지"


async def test_absorb_rejects_bad_pairs(db: AsyncSession, ctx: Ctx) -> None:
    from app.services import provisional_absorb_service as absorb_svc
    from app.utils.exceptions import BadRequestError

    real_id = await _make_real_user(db, ctx, "x1")
    await db.commit()
    ghost = await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Ghost Bad", role_id=ctx.role_id
    )

    with pytest.raises(BadRequestError):  # 자기 자신
        await absorb_svc.preview_absorb(db, ctx.org_id, ghost.id, ghost.id)
    with pytest.raises(BadRequestError):  # 출발지가 유령이 아님
        await absorb_svc.preview_absorb(db, ctx.org_id, real_id, ghost.id)


# ---------------------------------------------------------------------------
# ⑦ 가입 소프트 가드 — 이름이 비슷한 유령이 있으면 인수 코드 사용을 안내
# ---------------------------------------------------------------------------


async def test_signup_warns_when_lookalike_provisional_exists(
    db: AsyncSession, ctx: Ctx
) -> None:
    from app.schemas.auth import RegisterRequest
    from app.services.auth_service import auth_service
    from app.utils.exceptions import ConflictError

    await prov_svc.create_provisional_user(
        db, ctx.org_id, full_name="Maria Santos", role_id=ctx.role_id
    )

    email = f"guard.{ctx.sfx}@example.com"
    token = await _issue_verification_token(db, email)

    def _req(**over):
        base = dict(
            username=f"guard_{ctx.sfx}",
            password="Str0ngPass!",
            full_name="Maria Santos",
            email=email,
            verification_token=token,
            store_ids=[],
        )
        base.update(over)
        return RegisterRequest(**base)

    # 이름이 겹치면 그냥 가입이 막히고 인수 코드를 안내한다
    with pytest.raises(ConflictError):
        await auth_service.app_register(db, _req(), ctx.org_id)

    # "아니오, 새 계정입니다" → 통과
    token2 = await _issue_verification_token(db, email)
    result = await auth_service.app_register(
        db, _req(skip_claim_check=True, verification_token=token2), ctx.org_id
    )
    assert result.access_token

    # 이름이 안 겹치면 가드가 걸리지 않는다
    email3 = f"other.{ctx.sfx}@example.com"
    token3 = await _issue_verification_token(db, email3)
    result3 = await auth_service.app_register(
        db,
        _req(
            username=f"other_{ctx.sfx}",
            full_name="Zebulon Quartzfield",
            email=email3,
            verification_token=token3,
        ),
        ctx.org_id,
    )
    assert result3.access_token
