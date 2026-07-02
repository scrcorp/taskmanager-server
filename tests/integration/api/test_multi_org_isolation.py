"""멀티-org 격리 테스트 (Model B 이행).

검증:
    - 2번째 org 존재 시 username(전역) 로그인이 동작 (resolve_company_code None 허용).
    - 매장 목록은 자기 org 만 (cross-org 미노출).
    - 타 org 의 store_id 를 직접 접근하면 404 (check_store_access org 바인딩 — Owner IDOR 수정).

conftest 는 단일 org(org1)를 세션 시드한다. 이 파일은 함수 스코프로 org2 를
만들고 종료 시 삭제(org CASCADE 로 roles/users/stores/role_permissions 정리).
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.organization import Organization, Store
from app.models.permission import Permission, RolePermission
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
        await db.commit()
        await db.refresh(org)
        await db.refresh(store)
        data = {"org_id": org.id, "store_id": store.id, "username": "org2owner"}

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
