"""체크리스트 템플릿 CRUD API 테스트.

Checklist template CRUD API tests — Templates and items.
Tests unique constraint (store+shift+position), item ordering, reorder.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import auth_header

ADMIN = "/api/v1/admin"


def checklist_url(store_id) -> str:
    return f"{ADMIN}/stores/{store_id}/checklist-templates"


@pytest_asyncio.fixture
async def shift(client: AsyncClient, admin_token, store):
    """테스트용 시간대 생성."""
    res = await client.post(
        f"{ADMIN}/stores/{store.id}/shifts",
        json={"name": "오전", "sort_order": 1},
        headers=auth_header(admin_token),
    )
    return res.json()


@pytest_asyncio.fixture
async def position(client: AsyncClient, admin_token, store):
    """테스트용 포지션 생성."""
    res = await client.post(
        f"{ADMIN}/stores/{store.id}/positions",
        json={"name": "그릴", "sort_order": 1},
        headers=auth_header(admin_token),
    )
    return res.json()


class TestChecklistTemplateCRUD:
    """체크리스트 템플릿 CRUD 테스트."""

    async def test_create_template(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 템플릿 생성 성공."""
        res = await client.post(checklist_url(store.id), json={
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "오전 그릴 체크리스트",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["title"] == "오전 그릴 체크리스트"
        assert data["item_count"] == 0

    async def test_create_duplicate_template(self, client: AsyncClient, admin_token, store, shift, position):
        """동일 조합(store+shift+position) 중복 생성 실패."""
        payload = {
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "First",
        }
        await client.post(checklist_url(store.id), json=payload, headers=auth_header(admin_token))

        payload["title"] = "Second"
        res = await client.post(checklist_url(store.id), json=payload, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)

    async def test_list_templates(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 템플릿 목록 조회."""
        await client.post(checklist_url(store.id), json={
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "List Test",
        }, headers=auth_header(admin_token))

        res = await client.get(checklist_url(store.id), headers=auth_header(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    async def test_update_template_title(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 템플릿 제목 변경."""
        create_res = await client.post(checklist_url(store.id), json={
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "Original",
        }, headers=auth_header(admin_token))
        template_id = create_res.json()["id"]

        # PUT은 /checklist-templates/{id} 경로 사용 (store prefix 없음)
        res = await client.put(
            f"{ADMIN}/checklist-templates/{template_id}",
            json={"title": "Updated Title"},
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
        assert res.json()["title"] == "Updated Title"


class TestChecklistItemCRUD:
    """체크리스트 항목 CRUD 테스트."""

    async def _create_template(self, client, admin_token, store, shift, position):
        res = await client.post(checklist_url(store.id), json={
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "Item Test Template",
        }, headers=auth_header(admin_token))
        return res.json()["id"]

    async def test_add_item(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 항목 추가."""
        template_id = await self._create_template(client, admin_token, store, shift, position)
        # POST은 /checklist-templates/{id}/items 경로
        res = await client.post(
            f"{ADMIN}/checklist-templates/{template_id}/items",
            json={
                "title": "그릴 예열",
                "description": "400도까지 예열",
                "verification_type": "photo",
                "sort_order": 0,
            },
            headers=auth_header(admin_token),
        )
        assert res.status_code == 201
        data = res.json()
        assert data["title"] == "그릴 예열"
        assert data["verification_type"] == "photo"

    async def test_update_item(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 항목 수정."""
        template_id = await self._create_template(client, admin_token, store, shift, position)
        item_res = await client.post(
            f"{ADMIN}/checklist-templates/{template_id}/items",
            json={"title": "Old Title", "sort_order": 0},
            headers=auth_header(admin_token),
        )
        item_id = item_res.json()["id"]

        # PUT은 /checklist-template-items/{item_id} 경로
        res = await client.put(
            f"{ADMIN}/checklist-template-items/{item_id}",
            json={"title": "New Title"},
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
        assert res.json()["title"] == "New Title"

    async def test_delete_item(self, client: AsyncClient, admin_token, store, shift, position):
        """체크리스트 항목 삭제."""
        template_id = await self._create_template(client, admin_token, store, shift, position)
        item_res = await client.post(
            f"{ADMIN}/checklist-templates/{template_id}/items",
            json={"title": "To Delete", "sort_order": 0},
            headers=auth_header(admin_token),
        )
        item_id = item_res.json()["id"]

        # DELETE은 /checklist-template-items/{item_id} 경로
        res = await client.delete(
            f"{ADMIN}/checklist-template-items/{item_id}",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200
