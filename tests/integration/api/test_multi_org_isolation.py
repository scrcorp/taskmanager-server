"""멀티-org 격리 테스트 (Model B 이행).

검증:
    - 2번째 org 존재 시 username(전역) 로그인이 동작 (resolve_company_code None 허용).
    - 매장 목록은 자기 org 만 (cross-org 미노출).
    - 타 org 의 store_id 를 직접 접근하면 404 (check_store_access org 바인딩 — Owner IDOR 수정).

conftest 는 단일 org(org1)를 세션 시드한다. 이 파일은 함수 스코프로 org2 를
만들고 종료 시 삭제(org CASCADE 로 roles/users/stores/role_permissions 정리).
"""

from __future__ import annotations

from datetime import date, time
from typing import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.database import async_session
from app.main import app
from app.models.organization import Organization, Store
from app.models.permission import Permission, RolePermission
from app.models.schedule import Schedule
from app.models.user import Role, User
from app.utils.password import hash_password


@pytest_asyncio.fixture
async def org2() -> AsyncIterator[dict]:
    """두 번째 org — super_owner role(전 권한) + owner user(org2owner/1234) + store 1개.

    종료 시 org 삭제로 하위(roles/users/stores/role_permissions) 일괄 정리.
    """
    async with async_session() as db:
        org = Organization(name="Second Org Inc")
        db.add(org)
        await db.flush()

        role = Role(organization_id=org.id, name="super_owner", priority=5)
        db.add(role)
        await db.flush()

        # 콘솔 로그인은 role 에 permission 이 1개 이상 있어야 통과 → 전 권한 부여.
        perm_ids = (await db.execute(select(Permission.id))).scalars().all()
        for pid in perm_ids:
            db.add(RolePermission(role_id=role.id, permission_id=pid))

        user = User(
            organization_id=org.id,
            role_id=role.id,
            username="org2owner",
            full_name="Org2 Owner",
            password_hash=hash_password("1234"),
            is_active=True,
        )
        db.add(user)

        store = Store(organization_id=org.id, name="Org2 Downtown", timezone="UTC")
        db.add(store)
        await db.flush()

        # org2 의 스케줄 신청(requested) — cross-org IDOR 검증용.
        sched = Schedule(
            organization_id=org.id,
            user_id=user.id,
            store_id=store.id,
            work_date=date(2026, 1, 15),
            start_time=time(9, 0),
            end_time=time(17, 0),
            status="requested",
        )
        db.add(sched)
        await db.commit()
        await db.refresh(org)
        await db.refresh(store)
        await db.refresh(sched)
        data = {
            "org_id": org.id,
            "store_id": store.id,
            "username": "org2owner",
            "user_id": user.id,
            "role_id": role.id,
            "request_id": sched.id,
        }

    try:
        yield data
    finally:
        async with async_session() as db:
            await db.execute(delete(Organization).where(Organization.id == data["org_id"]))
            await db.commit()


async def _login(username: str, password: str = "1234") -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": username, "password": password},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_second_org_owner_can_login_by_username(org2: dict):
    """2번째 org 존재해도 username 전역 로그인 동작 (company_code 불필요)."""
    token = await _login("org2owner")
    assert token


async def test_store_list_is_org_scoped(org2: dict, async_client: AsyncClient, admin_headers: dict):
    """각 org 는 자기 매장만 본다 — cross-org 미노출."""
    # org2 owner: org2 store 만
    org2_token = await _login("org2owner")
    r2 = await async_client.get(
        "/api/v1/console/stores", headers={"Authorization": f"Bearer {org2_token}"}
    )
    assert r2.status_code == 200, r2.text
    ids2 = {s["id"] for s in r2.json()}
    assert str(org2["store_id"]) in ids2
    assert len(ids2) == 1  # org2 는 매장 1개뿐

    # org1 admin(testadmin): org2 store 는 안 보임
    r1 = await async_client.get("/api/v1/console/stores", headers=admin_headers)
    assert r1.status_code == 200, r1.text
    ids1 = {s["id"] for s in r1.json()}
    assert str(org2["store_id"]) not in ids1


async def test_cross_org_store_detail_is_404(org2: dict, async_client: AsyncClient, admin_headers: dict):
    """org1 super_owner 가 org2 의 store_id 를 직접 조회 → 404 (Owner IDOR 차단)."""
    resp = await async_client.get(
        f"/api/v1/console/stores/{org2['store_id']}", headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


async def test_cross_org_schedule_request_status_change_is_404(
    org2: dict, async_client: AsyncClient, admin_headers: dict
):
    """org1 admin 이 org2 의 schedule request 상태를 변경 시도 → 404 (write IDOR 차단)."""
    resp = await async_client.patch(
        f"/api/v1/console/schedule-requests/{org2['request_id']}/status",
        headers=admin_headers,
        json={"status": "rejected"},
    )
    assert resp.status_code == 404, resp.text

    # org2 의 request 는 변경되지 않았어야 함 (여전히 requested).
    async with async_session() as db:
        sched = (
            await db.execute(select(Schedule).where(Schedule.id == org2["request_id"]))
        ).scalar_one()
        assert sched.status == "requested"


async def test_cross_org_user_detail_is_404(org2: dict, async_client: AsyncClient, admin_headers: dict):
    """org1 admin 이 org2 의 user_id 를 직접 조회 → 404 (users 도메인 org 스코프)."""
    resp = await async_client.get(
        f"/api/v1/console/users/{org2['user_id']}", headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


# ── 역방향(org2 → org1) 격리: org2 owner 가 org1 자원 접근 시 차단 ──────────


async def test_org2_owner_cannot_read_org1_store(
    org2: dict, async_client: AsyncClient, test_store_id
):
    """org2 owner 가 org1 의 store_id 를 조회 → 404."""
    token = await _login("org2owner")
    resp = await async_client.get(
        f"/api/v1/console/stores/{test_store_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


async def test_org2_owner_cannot_read_org1_user(
    org2: dict, async_client: AsyncClient, test_users: dict
):
    """org2 owner 가 org1 의 user_id 를 조회 → 404."""
    org1_user_id = test_users["testgm"]["id"]
    token = await _login("org2owner")
    resp = await async_client.get(
        f"/api/v1/console/users/{org1_user_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


async def test_forged_org_in_token_is_rejected(
    org2: dict, async_client: AsyncClient, test_users: dict
):
    """멤버십이 있는 계정의 토큰에 비-멤버 org 를 위조하면 거부 (Model B org 컨텍스트 검증)."""
    from app.utils.jwt import create_access_token

    admin_id = test_users["testadmin"]["id"]
    forged = create_access_token(
        {"sub": str(admin_id), "org": str(org2["org_id"]), "role": "super_owner", "priority": 5}
    )
    resp = await async_client.get(
        "/api/v1/console/stores", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 403, resp.text


@pytest_asyncio.fixture
async def dual_member_user(org2: dict, seed_roles: dict, seed_organization: dict) -> AsyncIterator[dict]:
    """org1(owner) + org2(super_owner) 양쪽 멤버십을 가진 user — 컨텍스트 전환 검증용.

    users.organization_id 는 org1(기본). 종료 시 user 삭제(멤버십 CASCADE 정리).
    """
    from app.models.org_member import OrgMember

    async with async_session() as db:
        u = User(
            organization_id=seed_organization["id"],
            role_id=seed_roles["owner"],
            username="dualmember",
            full_name="Dual Member",
            password_hash=hash_password("1234"),
            is_active=True,
        )
        db.add(u)
        await db.flush()
        db.add(OrgMember(user_id=u.id, organization_id=seed_organization["id"], role_id=seed_roles["owner"]))
        db.add(OrgMember(user_id=u.id, organization_id=org2["org_id"], role_id=org2["role_id"]))
        await db.commit()
        await db.refresh(u)
        data = {"user_id": u.id}
    try:
        yield data
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == data["user_id"]))
            await db.commit()


async def test_context_org_follows_selected_membership(
    org2: dict,
    dual_member_user: dict,
    async_client: AsyncClient,
    test_store_id,
    seed_organization: dict,
):
    """같은 계정이 토큰의 org 에 따라 그 org 컨텍스트(매장목록)로 동작 — 컨텍스트 전환."""
    from app.utils.jwt import create_access_token

    uid = str(dual_member_user["user_id"])
    tok_org1 = create_access_token({"sub": uid, "org": str(seed_organization["id"])})
    tok_org2 = create_access_token({"sub": uid, "org": str(org2["org_id"])})

    r1 = await async_client.get(
        "/api/v1/console/stores", headers={"Authorization": f"Bearer {tok_org1}"}
    )
    assert r1.status_code == 200, r1.text
    ids1 = {s["id"] for s in r1.json()}
    assert str(test_store_id) in ids1
    assert str(org2["store_id"]) not in ids1

    r2 = await async_client.get(
        "/api/v1/console/stores", headers={"Authorization": f"Bearer {tok_org2}"}
    )
    assert r2.status_code == 200, r2.text
    ids2 = {s["id"] for s in r2.json()}
    assert str(org2["store_id"]) in ids2
    assert str(test_store_id) not in ids2


async def test_created_user_gets_org_member(
    async_client: AsyncClient, admin_headers: dict, seed_roles: dict, seed_organization: dict
):
    """console 로 유저 생성 시 org_member 도 함께 생성된다 (Model B 완결 엔티티)."""
    import uuid as _uuid
    from app.models.org_member import OrgMember

    uname = f"mbtest_{_uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={"username": uname, "password": "test1234", "full_name": "MB Test", "role_id": str(seed_roles["staff"])},
    )
    assert resp.status_code in (200, 201), resp.text
    uid = _uuid.UUID(resp.json()["id"])
    try:
        async with async_session() as db:
            m = (
                await db.execute(select(OrgMember).where(OrgMember.user_id == uid))
            ).scalar_one_or_none()
            assert m is not None, "org_member not created"
            assert m.organization_id == seed_organization["id"]
            assert m.status == "active"
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_home_org_uses_live_user_role_not_stale_member(
    async_client: AsyncClient, seed_roles: dict, seed_organization: dict
):
    """org_member 가 stale(staff) 여도 home org 컨텍스트는 users.role_id(라이브 owner)를 쓴다.

    회귀 방지: home org 에서 org_member.role 을 신뢰하면, role 변경 후 org_member 미동기화 시
    stale role 이 적용되는 버그가 생긴다. home org 는 override 하지 않아야 한다.
    """
    import uuid as _uuid
    from app.models.org_member import OrgMember

    uid = _uuid.uuid4()
    uname = f"stale_{_uuid.uuid4().hex[:6]}"
    async with async_session() as db:
        db.add(User(id=uid, organization_id=seed_organization["id"], role_id=seed_roles["staff"],
                    username=uname, full_name="Stale Role", password_hash=hash_password("1234"), is_active=True))
        db.add(OrgMember(user_id=uid, organization_id=seed_organization["id"], role_id=seed_roles["staff"]))
        await db.commit()
    try:
        # users.role_id 를 owner 로 승격 (org_member 는 staff 로 stale 하게 방치)
        async with async_session() as db:
            await db.execute(update(User).where(User.id == uid).values(role_id=seed_roles["owner"]))
            await db.commit()
        # 로그인(owner 라 console 가능) + owner-gated 조회 → 200 (live owner). stale staff 면 403.
        token = await _login(uname)
        r = await async_client.get(
            "/api/v1/console/stores", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_context_switch_does_not_corrupt_home_org(
    org2: dict, dual_member_user: dict, async_client: AsyncClient
):
    """org2 컨텍스트로 요청해도 계정의 DB home org(org1)는 변하지 않아야 함 (flush 안 됨)."""
    from app.utils.jwt import create_access_token
    from app.models.user import User as U

    uid = dual_member_user["user_id"]
    tok_org2 = create_access_token({"sub": str(uid), "org": str(org2["org_id"])})
    # org2 컨텍스트로 임의 요청
    await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {tok_org2}"})
    # DB 의 home org 는 여전히 org1 이어야 함
    async with async_session() as db:
        u = (await db.execute(select(U).where(U.id == uid))).scalar_one()
        assert u.organization_id != org2["org_id"]  # home 그대로
