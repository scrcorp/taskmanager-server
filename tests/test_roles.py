"""역할 CRUD API 테스트.

Role CRUD API tests — Create, Read, Update, Delete role endpoints.
Tests unique constraints (org-name, org-level) and authorization.
"""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header

URL = "/api/v1/admin/roles/"


class TestRoleCreate:
    """역할 생성 테스트."""

    async def test_create_role(self, client: AsyncClient, admin_token, org, roles):
        """새 역할 생성 성공."""
        res = await client.post(URL, json={
            "name": "intern",
            "level": 5,
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "intern"
        assert data["level"] == 5

    async def test_create_duplicate_name_fails(self, client: AsyncClient, admin_token, org, roles):
        """동일 조직 내 중복 역할 이름 생성 실패."""
        res = await client.post(URL, json={
            "name": "admin",
            "level": 10,
        }, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)

    async def test_create_duplicate_level_fails(self, client: AsyncClient, admin_token, org, roles):
        """동일 조직 내 중복 레벨 생성 실패."""
        res = await client.post(URL, json={
            "name": "unique_name",
            "level": 1,
        }, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)

    async def test_create_role_staff_forbidden(self, client: AsyncClient, staff_token):
        """스태프 권한으로 역할 생성 시 403."""
        res = await client.post(URL, json={
            "name": "hacker",
            "level": 0,
        }, headers=auth_header(staff_token))
        assert res.status_code == 403


class TestRoleRead:
    """역할 조회 테스트."""

    async def test_list_roles(self, client: AsyncClient, admin_token, roles):
        """역할 목록 조회."""
        res = await client.get(URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 4
        names = {r["name"] for r in data}
        assert {"admin", "manager", "supervisor", "staff"}.issubset(names)


class TestRoleUpdate:
    """역할 수정 테스트."""

    async def test_update_role_name(self, client: AsyncClient, admin_token, roles):
        """역할 이름 변경."""
        role_id = str(roles["supervisor"].id)
        res = await client.put(f"{URL}{role_id}", json={
            "name": "team_lead",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["name"] == "team_lead"

    async def test_update_nonexistent_role(self, client: AsyncClient, admin_token, org, roles):
        """존재하지 않는 역할 수정 시 404."""
        fake_id = str(uuid.uuid4())
        res = await client.put(f"{URL}{fake_id}", json={
            "name": "ghost",
        }, headers=auth_header(admin_token))
        assert res.status_code == 404


class TestRoleDelete:
    """역할 삭제 테스트."""

    async def test_delete_role(self, client: AsyncClient, admin_token, org, roles):
        """사용자가 없는 역할 삭제 성공."""
        # 임시 역할 생성 후 삭제
        create_res = await client.post(URL, json={
            "name": "temp_role",
            "level": 99,
        }, headers=auth_header(admin_token))
        role_id = create_res.json()["id"]

        res = await client.delete(f"{URL}{role_id}", headers=auth_header(admin_token))
        assert res.status_code == 204
