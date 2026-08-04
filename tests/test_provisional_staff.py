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
