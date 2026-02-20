"""공지사항/추가업무 API 테스트.

Announcement and Additional Task API tests.
Tests CRUD, store scoping, assignee management, and authorization.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import auth_header

ANNOUNCE_URL = "/api/v1/admin/announcements/"
TASK_URL = "/api/v1/admin/additional-tasks/"


# ===== Announcements =====

class TestAnnouncementCRUD:
    """공지사항 CRUD 테스트."""

    async def test_create_org_wide_announcement(self, client: AsyncClient, admin_token):
        """조직 전체 공지 생성."""
        res = await client.post(ANNOUNCE_URL, json={
            "title": "전체 공지",
            "content": "중요한 안내사항입니다.",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["title"] == "전체 공지"
        assert data["store_id"] is None

    async def test_create_store_announcement(self, client: AsyncClient, admin_token, store):
        """특정 매장 공지 생성."""
        res = await client.post(ANNOUNCE_URL, json={
            "title": "매장 공지",
            "content": "매장 전용 안내.",
            "store_id": str(store.id),
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        assert res.json()["store_id"] == str(store.id)

    async def test_list_announcements(self, client: AsyncClient, admin_token):
        """공지사항 목록 조회."""
        await client.post(ANNOUNCE_URL, json={
            "title": "List Test",
            "content": "Test content",
        }, headers=auth_header(admin_token))

        res = await client.get(ANNOUNCE_URL, headers=auth_header(admin_token))
        assert res.status_code == 200

    async def test_update_announcement(self, client: AsyncClient, admin_token):
        """공지사항 수정."""
        create_res = await client.post(ANNOUNCE_URL, json={
            "title": "Original",
            "content": "Original content",
        }, headers=auth_header(admin_token))
        ann_id = create_res.json()["id"]

        res = await client.put(f"/api/v1/admin/announcements/{ann_id}", json={
            "title": "Updated Title",
            "content": "Updated content",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        assert res.json()["title"] == "Updated Title"

    async def test_delete_announcement(self, client: AsyncClient, admin_token):
        """공지사항 삭제."""
        create_res = await client.post(ANNOUNCE_URL, json={
            "title": "To Delete",
            "content": "Will be deleted",
        }, headers=auth_header(admin_token))
        ann_id = create_res.json()["id"]

        res = await client.delete(f"/api/v1/admin/announcements/{ann_id}", headers=auth_header(admin_token))
        assert res.status_code == 200

    async def test_announcement_staff_forbidden(self, client: AsyncClient, staff_token):
        """스태프 권한으로 공지 생성 시 403."""
        res = await client.post(ANNOUNCE_URL, json={
            "title": "Hack",
            "content": "Hacked",
        }, headers=auth_header(staff_token))
        assert res.status_code == 403


# ===== Additional Tasks =====

class TestAdditionalTaskCRUD:
    """추가 업무 CRUD 테스트."""

    async def test_create_task(self, client: AsyncClient, admin_token, staff_user):
        """추가 업무 생성 성공."""
        res = await client.post(TASK_URL, json={
            "title": "재고 확인",
            "description": "전 매장 재고 확인 필요",
            "priority": "urgent",
            "assignee_ids": [str(staff_user.id)],
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        data = res.json()
        assert data["title"] == "재고 확인"
        assert data["priority"] == "urgent"
        assert data["status"] == "pending"

    async def test_create_task_with_store(self, client: AsyncClient, admin_token, store, staff_user):
        """매장 지정 추가 업무 생성."""
        res = await client.post(TASK_URL, json={
            "title": "매장 점검",
            "store_id": str(store.id),
            "assignee_ids": [str(staff_user.id)],
        }, headers=auth_header(admin_token))
        assert res.status_code == 201
        assert res.json()["store_id"] == str(store.id)

    async def test_create_task_no_assignees(self, client: AsyncClient, admin_token):
        """담당자 없이 업무 생성."""
        res = await client.post(TASK_URL, json={
            "title": "일반 업무",
        }, headers=auth_header(admin_token))
        assert res.status_code == 201

    async def test_list_tasks(self, client: AsyncClient, admin_token, staff_user):
        """추가 업무 목록 조회."""
        await client.post(TASK_URL, json={
            "title": "List Task",
        }, headers=auth_header(admin_token))

        res = await client.get(TASK_URL, headers=auth_header(admin_token))
        assert res.status_code == 200

    async def test_update_task(self, client: AsyncClient, admin_token):
        """추가 업무 수정."""
        create_res = await client.post(TASK_URL, json={
            "title": "Original Task",
        }, headers=auth_header(admin_token))
        task_id = create_res.json()["id"]

        res = await client.put(f"/api/v1/admin/additional-tasks/{task_id}", json={
            "title": "Updated Task",
            "priority": "urgent",
            "status": "in_progress",
        }, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Updated Task"
        assert data["priority"] == "urgent"
        assert data["status"] == "in_progress"

    async def test_delete_task(self, client: AsyncClient, admin_token):
        """추가 업무 삭제."""
        create_res = await client.post(TASK_URL, json={
            "title": "To Delete Task",
        }, headers=auth_header(admin_token))
        task_id = create_res.json()["id"]

        res = await client.delete(f"/api/v1/admin/additional-tasks/{task_id}", headers=auth_header(admin_token))
        assert res.status_code == 200

    async def test_task_staff_forbidden(self, client: AsyncClient, staff_token):
        """스태프 권한으로 업무 생성 시 403."""
        res = await client.post(TASK_URL, json={
            "title": "Hack Task",
        }, headers=auth_header(staff_token))
        assert res.status_code == 403
