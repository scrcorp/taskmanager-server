"""브랜드 CRUD API 테스트.

Brand CRUD API tests — Create, Read, Update, Delete brand endpoints.
Tests authorization (admin/manager only), validation, and edge cases.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header

URL = "/api/v1/admin/brands/"


class TestBrandCreate:
    """브랜드 생성 테스트."""

    async def test_create_brand(self, client: AsyncClient, admin_token):
        """브랜드 생성 성공."""
        res = await client.post(URL, json={
            "name": "New Brand",
            "address": "456 New St",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "New Brand"
        assert data["address"] == "456 New St"
        assert data["is_active"] is True

    async def test_create_brand_without_address(self, client: AsyncClient, admin_token):
        """주소 없이 브랜드 생성."""
        res = await client.post(URL, json={
            "name": "No Address Brand",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        assert res.json()["address"] is None

    async def test_create_brand_staff_forbidden(self, client: AsyncClient, staff_token):
        """스태프 권한으로 브랜드 생성 시 403."""
        res = await client.post(URL, json={
            "name": "Forbidden Brand",
        }, headers=auth_header(staff_token))
        assert res.status_code == 403

    async def test_create_brand_no_auth(self, client: AsyncClient):
        """인증 없이 브랜드 생성 시 403."""
        res = await client.post(URL, json={"name": "X"})
        assert res.status_code == 403


class TestBrandRead:
    """브랜드 조회 테스트."""

    async def test_list_brands(self, client: AsyncClient, brand, admin_token):
        """브랜드 목록 조회."""
        res = await client.get(URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(b["name"] == "Test Brand" for b in data)

    async def test_get_brand_detail(self, client: AsyncClient, brand, admin_token):
        """브랜드 상세 조회 (shifts/positions 포함)."""
        res = await client.get(f"{URL}{brand.id}", headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Test Brand"
        assert "shifts" in data
        assert "positions" in data

    async def test_get_nonexistent_brand(self, client: AsyncClient, admin_token):
        """존재하지 않는 브랜드 조회 시 404."""
        import uuid
        fake_id = str(uuid.uuid4())
        res = await client.get(f"{URL}{fake_id}", headers=auth_header(admin_token))
        assert res.status_code == 404


class TestBrandUpdate:
    """브랜드 수정 테스트."""

    async def test_update_brand_name(self, client: AsyncClient, brand, admin_token):
        """브랜드 이름 수정."""
        res = await client.put(f"{URL}{brand.id}", json={
            "name": "Updated Brand",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["name"] == "Updated Brand"

    async def test_update_brand_partial(self, client: AsyncClient, brand, admin_token):
        """부분 업데이트 — 주소만 변경."""
        res = await client.put(f"{URL}{brand.id}", json={
            "address": "789 Updated Ave",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["address"] == "789 Updated Ave"

    async def test_deactivate_brand(self, client: AsyncClient, brand, admin_token):
        """브랜드 비활성화."""
        res = await client.put(f"{URL}{brand.id}", json={
            "is_active": False,
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["is_active"] is False


class TestBrandDelete:
    """브랜드 삭제 테스트."""

    async def test_delete_brand(self, client: AsyncClient, brand, admin_token):
        """브랜드 삭제 성공."""
        res = await client.delete(f"{URL}{brand.id}", headers=auth_header(admin_token))
        assert res.status_code == 204

        # 삭제 후 조회 시 404
        res2 = await client.get(f"{URL}{brand.id}", headers=auth_header(admin_token))
        assert res2.status_code == 404

    async def test_delete_nonexistent_brand(self, client: AsyncClient, admin_token):
        """존재하지 않는 브랜드 삭제 시 404."""
        import uuid
        fake_id = str(uuid.uuid4())
        res = await client.delete(f"{URL}{fake_id}", headers=auth_header(admin_token))
        assert res.status_code == 404
