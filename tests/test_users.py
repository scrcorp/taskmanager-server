"""사용자 CRUD API 테스트.

User CRUD API tests — Create, List, Update, Toggle active, brand assignment.
Tests authorization, unique username constraint, and role-level restrictions.
"""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header

URL = "/api/v1/admin/users/"


class TestUserCreate:
    """사용자 생성 테스트."""

    async def test_create_user(self, client: AsyncClient, admin_token, roles):
        """사용자 생성 성공."""
        res = await client.post(URL, json={
            "username": "newuser",
            "password": "secure123!",
            "full_name": "New User",
            "email": "new@example.com",
            "role_id": str(roles["staff"].id),
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["username"] == "newuser"
        assert data["full_name"] == "New User"
        assert data["role_name"] == "staff"
        assert data["is_active"] is True

    async def test_create_user_duplicate_username(self, client: AsyncClient, admin_token, admin_user, roles):
        """중복 사용자명 생성 실패."""
        res = await client.post(URL, json={
            "username": "admin",
            "password": "whatever",
            "full_name": "Dup Admin",
            "role_id": str(roles["staff"].id),
        }, headers=auth_header(admin_token))
        assert res.status_code == 409

    async def test_create_user_manager_allowed(self, client: AsyncClient, manager_token, roles):
        """매니저도 사용자 생성 가능."""
        res = await client.post(URL, json={
            "username": "mgr_created",
            "password": "pass123!",
            "full_name": "Manager Created",
            "role_id": str(roles["staff"].id),
        }, headers=auth_header(manager_token))
        assert res.status_code == 201

    async def test_create_user_staff_forbidden(self, client: AsyncClient, staff_token, roles):
        """스태프 권한으로 사용자 생성 시 403."""
        res = await client.post(URL, json={
            "username": "hacker",
            "password": "hack123!",
            "full_name": "Hacker",
            "role_id": str(roles["staff"].id),
        }, headers=auth_header(staff_token))
        assert res.status_code == 403


class TestUserRead:
    """사용자 조회 테스트."""

    async def test_list_users(self, client: AsyncClient, admin_token, admin_user, staff_user):
        """사용자 목록 조회."""
        res = await client.get(URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 2

    async def test_get_user_detail(self, client: AsyncClient, admin_token, staff_user):
        """사용자 상세 조회."""
        res = await client.get(f"{URL}{staff_user.id}", headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["username"] == "staff"

    async def test_get_nonexistent_user(self, client: AsyncClient, admin_token):
        """존재하지 않는 사용자 조회 시 404."""
        fake_id = str(uuid.uuid4())
        res = await client.get(f"{URL}{fake_id}", headers=auth_header(admin_token))
        assert res.status_code == 404


class TestUserUpdate:
    """사용자 수정 테스트."""

    async def test_update_user_name(self, client: AsyncClient, admin_token, staff_user):
        """사용자 이름 수정."""
        res = await client.put(f"{URL}{staff_user.id}", json={
            "full_name": "Updated Staff Name",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["full_name"] == "Updated Staff Name"

    async def test_toggle_user_active(self, client: AsyncClient, admin_token, staff_user):
        """사용자 활성/비활성 토글."""
        # 비활성화
        res = await client.put(f"{URL}{staff_user.id}", json={
            "is_active": False,
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["is_active"] is False


class TestUserBrandAssignment:
    """사용자 브랜드 배정 테스트."""

    async def test_assign_brand(self, client: AsyncClient, admin_token, staff_user, brand):
        """사용자에게 브랜드 배정."""
        res = await client.post(
            f"{URL}{staff_user.id}/brands/{brand.id}",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 201

    async def test_list_user_brands(self, client: AsyncClient, admin_token, staff_user, brand):
        """사용자 배정 브랜드 목록 조회."""
        # 먼저 배정
        await client.post(
            f"{URL}{staff_user.id}/brands/{brand.id}",
            headers=auth_header(admin_token),
        )
        res = await client.get(
            f"{URL}{staff_user.id}/brands",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
        assert isinstance(res.json(), list)
