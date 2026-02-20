"""매장 CRUD API 테스트.

Store CRUD API tests — Create, Read, Update, Delete store endpoints.
Tests authorization (admin/manager only), validation, and edge cases.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header

URL = "/api/v1/admin/stores/"


class TestStoreCreate:
    """매장 생성 테스트."""

    async def test_create_store(self, client: AsyncClient, admin_token):
        """매장 생성 성공."""
        res = await client.post(URL, json={
            "name": "New Store",
            "address": "456 New St",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "New Store"
        assert data["address"] == "456 New St"
        assert data["is_active"] is True

    async def test_create_store_without_address(self, client: AsyncClient, admin_token):
        """주소 없이 매장 생성."""
        res = await client.post(URL, json={
            "name": "No Address Store",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        assert res.json()["address"] is None

    async def test_create_store_staff_forbidden(self, client: AsyncClient, staff_token):
        """스태프 권한으로 매장 생성 시 403."""
        res = await client.post(URL, json={
            "name": "Forbidden Store",
        }, headers=auth_header(staff_token))
        assert res.status_code == 403

    async def test_create_store_no_auth(self, client: AsyncClient):
        """인증 없이 매장 생성 시 403."""
        res = await client.post(URL, json={"name": "X"})
        assert res.status_code == 403


class TestStoreRead:
    """매장 조회 테스트."""

    async def test_list_stores(self, client: AsyncClient, store, admin_token):
        """매장 목록 조회."""
        res = await client.get(URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(b["name"] == "Test Store" for b in data)

    async def test_get_store_detail(self, client: AsyncClient, store, admin_token):
        """매장 상세 조회 (shifts/positions 포함)."""
        res = await client.get(f"{URL}{store.id}", headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Test Store"
        assert "shifts" in data
        assert "positions" in data

    async def test_get_nonexistent_store(self, client: AsyncClient, admin_token):
        """존재하지 않는 매장 조회 시 404."""
        import uuid
        fake_id = str(uuid.uuid4())
        res = await client.get(f"{URL}{fake_id}", headers=auth_header(admin_token))
        assert res.status_code == 404


class TestStoreUpdate:
    """매장 수정 테스트."""

    async def test_update_store_name(self, client: AsyncClient, store, admin_token):
        """매장 이름 수정."""
        res = await client.put(f"{URL}{store.id}", json={
            "name": "Updated Store",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["name"] == "Updated Store"

    async def test_update_store_partial(self, client: AsyncClient, store, admin_token):
        """부분 업데이트 — 주소만 변경."""
        res = await client.put(f"{URL}{store.id}", json={
            "address": "789 Updated Ave",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["address"] == "789 Updated Ave"

    async def test_deactivate_store(self, client: AsyncClient, store, admin_token):
        """매장 비활성화."""
        res = await client.put(f"{URL}{store.id}", json={
            "is_active": False,
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["is_active"] is False


class TestStoreDelete:
    """매장 삭제 테스트."""

    async def test_delete_store(self, client: AsyncClient, store, admin_token):
        """매장 삭제 성공."""
        res = await client.delete(f"{URL}{store.id}", headers=auth_header(admin_token))
        assert res.status_code == 204

        # 삭제 후 조회 시 404
        res2 = await client.get(f"{URL}{store.id}", headers=auth_header(admin_token))
        assert res2.status_code == 404

    async def test_delete_nonexistent_store(self, client: AsyncClient, admin_token):
        """존재하지 않는 매장 삭제 시 404."""
        import uuid
        fake_id = str(uuid.uuid4())
        res = await client.delete(f"{URL}{fake_id}", headers=auth_header(admin_token))
        assert res.status_code == 404
