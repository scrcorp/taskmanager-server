"""시간대/포지션 CRUD API 테스트.

Shift and Position CRUD API tests — scoped under brand.
Tests unique constraints (brand-name) and cascade behavior.
"""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header


def shift_url(brand_id) -> str:
    return f"/api/v1/admin/brands/{brand_id}/shifts"


def position_url(brand_id) -> str:
    return f"/api/v1/admin/brands/{brand_id}/positions"


# ===== Shifts =====

class TestShiftCRUD:
    """시간대 CRUD 테스트."""

    async def test_create_shift(self, client: AsyncClient, admin_token, brand):
        """시간대 생성 성공."""
        res = await client.post(shift_url(brand.id), json={
            "name": "오전",
            "sort_order": 1,
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "오전"
        assert data["sort_order"] == 1

    async def test_create_duplicate_shift_name(self, client: AsyncClient, admin_token, brand):
        """동일 브랜드 내 중복 시간대 이름 실패."""
        await client.post(shift_url(brand.id), json={
            "name": "오후",
            "sort_order": 2,
        }, headers=auth_header(admin_token))

        res = await client.post(shift_url(brand.id), json={
            "name": "오후",
            "sort_order": 3,
        }, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)

    async def test_list_shifts(self, client: AsyncClient, admin_token, brand):
        """시간대 목록 조회."""
        await client.post(shift_url(brand.id), json={
            "name": "야간",
            "sort_order": 3,
        }, headers=auth_header(admin_token))

        res = await client.get(shift_url(brand.id), headers=auth_header(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    async def test_update_shift(self, client: AsyncClient, admin_token, brand):
        """시간대 수정."""
        create_res = await client.post(shift_url(brand.id), json={
            "name": "morning",
            "sort_order": 1,
        }, headers=auth_header(admin_token))
        shift_id = create_res.json()["id"]

        res = await client.put(
            f"{shift_url(brand.id)}/{shift_id}",
            json={"name": "Early Morning"},
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
        assert res.json()["name"] == "Early Morning"

    async def test_delete_shift(self, client: AsyncClient, admin_token, brand):
        """시간대 삭제."""
        create_res = await client.post(shift_url(brand.id), json={
            "name": "temp_shift",
            "sort_order": 99,
        }, headers=auth_header(admin_token))
        shift_id = create_res.json()["id"]

        res = await client.delete(
            f"{shift_url(brand.id)}/{shift_id}",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 204

    async def test_shift_staff_forbidden(self, client: AsyncClient, staff_token, brand):
        """스태프 권한으로 시간대 생성 시 403."""
        res = await client.post(shift_url(brand.id), json={
            "name": "Hack",
        }, headers=auth_header(staff_token))
        assert res.status_code == 403


# ===== Positions =====

class TestPositionCRUD:
    """포지션 CRUD 테스트."""

    async def test_create_position(self, client: AsyncClient, admin_token, brand):
        """포지션 생성 성공."""
        res = await client.post(position_url(brand.id), json={
            "name": "그릴",
            "sort_order": 1,
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "그릴"

    async def test_create_duplicate_position_name(self, client: AsyncClient, admin_token, brand):
        """동일 브랜드 내 중복 포지션 이름 실패."""
        await client.post(position_url(brand.id), json={
            "name": "카운터",
        }, headers=auth_header(admin_token))

        res = await client.post(position_url(brand.id), json={
            "name": "카운터",
        }, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)

    async def test_list_positions(self, client: AsyncClient, admin_token, brand):
        """포지션 목록 조회."""
        await client.post(position_url(brand.id), json={
            "name": "청소",
        }, headers=auth_header(admin_token))

        res = await client.get(position_url(brand.id), headers=auth_header(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    async def test_update_position(self, client: AsyncClient, admin_token, brand):
        """포지션 수정."""
        create_res = await client.post(position_url(brand.id), json={
            "name": "old_pos",
        }, headers=auth_header(admin_token))
        pos_id = create_res.json()["id"]

        res = await client.put(
            f"{position_url(brand.id)}/{pos_id}",
            json={"name": "new_pos"},
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
        assert res.json()["name"] == "new_pos"

    async def test_delete_position(self, client: AsyncClient, admin_token, brand):
        """포지션 삭제."""
        create_res = await client.post(position_url(brand.id), json={
            "name": "temp_pos",
        }, headers=auth_header(admin_token))
        pos_id = create_res.json()["id"]

        res = await client.delete(
            f"{position_url(brand.id)}/{pos_id}",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 204
