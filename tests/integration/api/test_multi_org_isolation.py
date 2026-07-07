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


async def test_crewid_assigned_sequentially_per_org(
    async_client: AsyncClient, admin_headers: dict, seed_roles: dict, seed_organization: dict
):
    """org_member 생성 시 crewid 가 그 org 의 다음 순번으로 부여된다."""
    import uuid as _uuid
    from app.models.org_member import OrgMember
    from app.services.org_numbering import next_crewid

    async with async_session() as db:
        expected = await next_crewid(db, seed_organization["id"])

    uname = f"crew_{_uuid.uuid4().hex[:6]}"
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={"username": uname, "password": "test1234", "full_name": "Crew", "role_id": str(seed_roles["staff"])},
    )
    assert resp.status_code in (200, 201), resp.text
    uid = _uuid.UUID(resp.json()["id"])
    try:
        async with async_session() as db:
            m = (await db.execute(select(OrgMember).where(OrgMember.user_id == uid))).scalar_one()
            assert m.crewid == expected
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_store_assignment_assigns_empid(
    async_client: AsyncClient, admin_headers: dict, seed_roles: dict, seed_organization: dict, test_store_id
):
    """사람을 매장에 배정하면 org_member_stores 행이 empid 부여되어 생성된다."""
    import uuid as _uuid
    from app.models.org_member import OrgMember, OrgMemberStore
    from app.services.user_service import user_service

    uname = f"emp_{_uuid.uuid4().hex[:6]}"
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={"username": uname, "password": "test1234", "full_name": "Emp Test", "role_id": str(seed_roles["staff"])},
    )
    assert resp.status_code in (200, 201), resp.text
    uid = _uuid.UUID(resp.json()["id"])
    try:
        async with async_session() as db:
            await user_service.add_user_store(db, uid, test_store_id, seed_organization["id"])
        async with async_session() as db:
            m = (await db.execute(select(OrgMember.id).where(OrgMember.user_id == uid))).scalar_one()
            oms = (
                await db.execute(
                    select(OrgMemberStore).where(
                        OrgMemberStore.org_member_id == m, OrgMemberStore.store_id == test_store_id
                    )
                )
            ).scalar_one()
            assert oms.empid is not None and oms.empid >= 1
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_empid_stable_across_reassignment(
    async_client: AsyncClient, admin_headers: dict, seed_roles: dict, seed_organization: dict, test_store_id
):
    """정책 A: 매장 배정 해제 후 재배정해도 empid 불변 (휴면 보존 → 재사용)."""
    import uuid as _uuid
    from app.models.org_member import OrgMember, OrgMemberStore
    from app.services.user_service import user_service

    uname = f"stable_{_uuid.uuid4().hex[:6]}"
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={"username": uname, "password": "test1234", "full_name": "Stable Emp", "role_id": str(seed_roles["staff"])},
    )
    uid = _uuid.UUID(resp.json()["id"])

    async def _empid() -> int | None:
        async with async_session() as db:
            m = (await db.execute(select(OrgMember.id).where(OrgMember.user_id == uid))).scalar_one()
            return (
                await db.execute(
                    select(OrgMemberStore.empid).where(
                        OrgMemberStore.org_member_id == m, OrgMemberStore.store_id == test_store_id
                    )
                )
            ).scalar_one_or_none()

    try:
        async with async_session() as db:
            await user_service.add_user_store(db, uid, test_store_id, seed_organization["id"])
        first = await _empid()
        assert first is not None
        # 해제 (휴면)
        async with async_session() as db:
            await user_service.remove_user_store(db, uid, test_store_id, seed_organization["id"])
        # 재배정 → 같은 empid
        async with async_session() as db:
            await user_service.add_user_store(db, uid, test_store_id, seed_organization["id"])
        second = await _empid()
        assert second == first, f"empid changed on reassignment: {first} -> {second}"
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_create_organization_bootstraps_and_owner_can_login(async_client: AsyncClient):
    """organization_service.create_organization 이 org+roles+권한+super_owner+org_member+store 를
    부트스트랩하고, 그 org 의 owner 가 실제로 로그인된다 (백오피스 org 생성의 핵심 로직)."""
    import uuid as _uuid
    from app.models.org_member import OrgMember
    from app.services.organization_service import organization_service

    uname = f"friend_{_uuid.uuid4().hex[:6]}"
    async with async_session() as db:
        res = await organization_service.create_organization(
            db,
            name="Friend Cafe",
            admin_username=uname,
            admin_password="pw123456",
            admin_email="f@example.com",
            timezone="America/New_York",
            first_store_name="Friend Downtown",
        )
    org_id = res["org_id"]
    try:
        async with async_session() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
            assert org.code and len(org.code) == 6
            assert org.timezone == "America/New_York"
            roles = (await db.execute(select(Role).where(Role.organization_id == org_id))).scalars().all()
            assert len(roles) == 5
            su = (
                await db.execute(select(User).where(User.organization_id == org_id, User.username == uname))
            ).scalar_one()
            m = (await db.execute(select(OrgMember).where(OrgMember.user_id == su.id))).scalar_one()
            assert m.organization_id == org_id and m.status == "active"
            assert res["store_id"] is not None
            store = (await db.execute(select(Store).where(Store.id == res["store_id"]))).scalar_one()
            assert store.organization_id == org_id
        # 신규 org owner 가 console 로그인 가능 (멀티-org → username 전역 해석)
        token = await _login(uname, "pw123456")
        assert token
        # 그리고 자기 org 매장만 본다
        r = await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        ids = {s["id"] for s in r.json()}
        assert str(res["store_id"]) in ids
    finally:
        async with async_session() as db:
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()


async def test_confirm_email_honors_test_code(async_client: AsyncClient):
    """로그인-후 이메일 인증(confirm-email)이 EMAIL_VERIFICATION_TEST_CODE(000000)를 존중한다.

    회귀: confirm_email 에 magic 우회가 없어 콘솔 verify-email 화면에서 000000 이 항상 실패했음.
    (env 의존 — TEST_CODE 미설정 환경에선 skip.)
    """
    import uuid as _uuid
    import pytest
    from app.config import settings
    from app.services.organization_service import organization_service

    if not (settings.EMAIL_VERIFICATION_TEST_CODE or "").strip():
        pytest.skip("EMAIL_VERIFICATION_TEST_CODE not set")

    uname = f"efix_{_uuid.uuid4().hex[:6]}"
    async with async_session() as db:
        res = await organization_service.create_organization(
            db, name="EFix", admin_username=uname, admin_password="pw123456", timezone="America/Los_Angeles",
        )
    org_id = res["org_id"]
    try:
        token = await _login(uname, "pw123456")
        r = await async_client.post(
            "/api/v1/app/auth/confirm-email",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": f"{uname}@test.com", "code": settings.EMAIL_VERIFICATION_TEST_CODE},
        )
        assert r.status_code == 200, r.text
        async with async_session() as db:
            u = (await db.execute(select(User).where(User.username == uname, User.organization_id == org_id))).scalar_one()
            assert u.email_verified is True
    finally:
        async with async_session() as db:
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()


async def test_suspended_license_blocks_org_access(org2: dict, async_client: AsyncClient):
    """org 라이센스 정지 → 그 org 사용자 접근 차단(403). 재활성 → 복구."""
    from app.models.license import License

    # org2 는 fixture 가 직접 만들어 라이센스가 없음 → active 로 신설
    async with async_session() as db:
        existing = (
            await db.execute(select(License).where(License.organization_id == org2["org_id"]))
        ).scalar_one_or_none()
        if existing is None:
            db.add(License(organization_id=org2["org_id"], status="active", plan="trial"))
            await db.commit()

    token = await _login("org2owner")
    # active → 접근 OK
    assert (await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {token}"})).status_code == 200

    # suspend → 즉시 403 (기존 토큰도 차단)
    async with async_session() as db:
        await db.execute(update(License).where(License.organization_id == org2["org_id"]).values(status="suspended"))
        await db.commit()
    r = await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    # 구조화된 에러 코드로 응답 (프론트 분기용)
    assert r.json()["detail"]["code"] == "ORG_LICENSE_INACTIVE", r.text

    # reactivate → 복구
    async with async_session() as db:
        await db.execute(update(License).where(License.organization_id == org2["org_id"]).values(status="active"))
        await db.commit()
    assert (await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_me_stays_200_and_reports_block_reason_when_license_suspended(
    org2: dict, async_client: AsyncClient
):
    """차단돼도 /me 는 200 — current_org_accessible=false + block_reason + org 목록을 준다."""
    from app.models.license import License

    async with async_session() as db:
        if (await db.execute(select(License).where(License.organization_id == org2["org_id"]))).scalar_one_or_none() is None:
            db.add(License(organization_id=org2["org_id"], status="active", plan="trial"))
            await db.commit()

    token = await _login("org2owner")
    hdr = {"Authorization": f"Bearer {token}"}
    # active
    r = await async_client.get("/api/v1/auth/me", headers=hdr)
    assert r.status_code == 200 and r.json()["current_org_accessible"] is True

    # suspend → /me 는 여전히 200, 차단이유 포함
    async with async_session() as db:
        await db.execute(update(License).where(License.organization_id == org2["org_id"]).values(status="suspended"))
        await db.commit()
    r2 = await async_client.get("/api/v1/auth/me", headers=hdr)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["current_org_accessible"] is False
    assert body["current_org_block_reason"] == "ORG_LICENSE_INACTIVE"
    entry = next((o for o in body["organizations"] if o["organization_id"] == str(org2["org_id"])), None)
    assert entry is not None and entry["accessible"] is False and entry["block_reason"] == "ORG_LICENSE_INACTIVE"

    async with async_session() as db:
        await db.execute(update(License).where(License.organization_id == org2["org_id"]).values(status="active"))
        await db.commit()


async def test_terminated_membership_is_access_revoked(
    async_client: AsyncClient, seed_roles: dict, seed_organization: dict
):
    """멤버십이 terminated 면(본인만 밴) ORG_ACCESS_REVOKED — 라이센스와 구분된 코드."""
    import uuid as _uuid
    from app.models.org_member import OrgMember

    uid = _uuid.uuid4()
    uname = f"banned_{_uuid.uuid4().hex[:6]}"
    async with async_session() as db:
        db.add(User(id=uid, organization_id=seed_organization["id"], role_id=seed_roles["owner"],
                    username=uname, full_name="Banned", password_hash=hash_password("1234"), is_active=True))
        db.add(OrgMember(user_id=uid, organization_id=seed_organization["id"], role_id=seed_roles["owner"], status="terminated"))
        await db.commit()
    try:
        token = await _login(uname)  # 로그인 자체는 멤버십 상태 안 봄
        hdr = {"Authorization": f"Bearer {token}"}
        # /me → 200 with ACCESS_REVOKED
        r = await async_client.get("/api/v1/auth/me", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["current_org_block_reason"] == "ORG_ACCESS_REVOKED"
        # org-scoped → 403 with ACCESS_REVOKED
        r2 = await async_client.get("/api/v1/console/stores", headers=hdr)
        assert r2.status_code == 403
        assert r2.json()["detail"]["code"] == "ORG_ACCESS_REVOKED"
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_switch_org_to_another_membership(
    dual_member_user: dict, org2: dict, async_client: AsyncClient
):
    """멀티-org 계정이 switch-org 로 다른 org 컨텍스트 토큰을 받고 그 org 데이터에 접근."""
    token = await _login("dualmember", "1234")
    r = await async_client.post(
        "/api/v1/auth/switch-org",
        headers={"Authorization": f"Bearer {token}"},
        json={"organization_id": str(org2["org_id"])},
    )
    assert r.status_code == 200, r.text
    token2 = r.json()["access_token"]
    rs = await async_client.get("/api/v1/console/stores", headers={"Authorization": f"Bearer {token2}"})
    assert rs.status_code == 200, rs.text
    assert str(org2["store_id"]) in {s["id"] for s in rs.json()}


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


# ── Attendance 기기 등록 코드: 조직별 발급 + 코드로 org 귀속 ──────────────────


async def test_create_organization_issues_attendance_code(async_client: AsyncClient):
    """create_organization 이 신규 org 에 attendance 등록 코드를 자동 발급한다."""
    import uuid as _uuid
    from app.core.access_code import get_code
    from app.services.organization_service import organization_service

    uname = f"acode_{_uuid.uuid4().hex[:6]}"
    async with async_session() as db:
        res = await organization_service.create_organization(
            db, name="ACode Cafe", admin_username=uname, admin_password="pw123456",
            timezone="America/New_York",
        )
    org_id = res["org_id"]
    try:
        async with async_session() as db:
            rec = await get_code(db, "attendance", org_id)
            assert rec is not None and rec.code and rec.organization_id == org_id
    finally:
        async with async_session() as db:
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()


async def _get_console_access_code(async_client: AsyncClient, token: str) -> str:
    r = await async_client.get(
        "/api/v1/console/access-codes/attendance",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["code"]


async def test_console_access_code_is_org_scoped(
    org2: dict, async_client: AsyncClient, admin_headers: dict
):
    """org1(testadmin) 과 org2 owner 는 서로 다른 등록 코드를 본다 (org 스코프)."""
    code1 = (await async_client.get("/api/v1/console/access-codes/attendance", headers=admin_headers)).json()["code"]
    token2 = await _login("org2owner")
    code2 = await _get_console_access_code(async_client, token2)
    assert code1 and code2 and code1 != code2


async def test_device_registers_to_org_of_submitted_code(
    org2: dict, async_client: AsyncClient
):
    """org2 코드로 기기 등록 → 기기가 org2 에 귀속 (가장 오래된 org 아님)."""
    import uuid as _uuid
    from app.models.attendance_device import AttendanceDevice

    token2 = await _login("org2owner")
    code2 = await _get_console_access_code(async_client, token2)

    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": code2, "fingerprint": "pytest-org2-device"},
    )
    assert resp.status_code == 201, resp.text
    device_id = _uuid.UUID(resp.json()["device_id"])
    try:
        async with async_session() as db:
            dev = (
                await db.execute(select(AttendanceDevice).where(AttendanceDevice.id == device_id))
            ).scalar_one()
            assert dev.organization_id == org2["org_id"]
    finally:
        async with async_session() as db:
            await db.execute(delete(AttendanceDevice).where(AttendanceDevice.id == device_id))
            await db.commit()


async def test_register_with_invalid_access_code_is_401(async_client: AsyncClient):
    """등록되지 않은 코드로 기기 등록 → 401 (조직 매칭 실패)."""
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": "ZZZZ99", "fingerprint": "pytest-bad"},
    )
    assert resp.status_code == 401, resp.text


async def test_console_rotate_is_org_isolated(
    org2: dict, async_client: AsyncClient, admin_headers: dict
):
    """org2 가 코드를 rotate 해도 org1 코드는 그대로."""
    code1_before = (await async_client.get("/api/v1/console/access-codes/attendance", headers=admin_headers)).json()["code"]
    token2 = await _login("org2owner")
    code2_before = await _get_console_access_code(async_client, token2)

    rot = await async_client.post(
        "/api/v1/console/access-codes/attendance/rotate",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert rot.status_code == 200, rot.text
    code2_after = rot.json()["code"]
    code1_after = (await async_client.get("/api/v1/console/access-codes/attendance", headers=admin_headers)).json()["code"]

    assert code2_after != code2_before       # org2 는 바뀜
    assert code1_after == code1_before        # org1 은 불변 (격리)
