"""근무 배정 API 테스트.

Work assignment API tests — Create, list, detail, checklist item completion.
Tests JSONB snapshot creation, duplicate assignment prevention, and pagination.
"""

import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import auth_header

URL = "/api/v1/admin/work-assignments/"


@pytest_asyncio.fixture
async def work_setup(client: AsyncClient, admin_token, store, staff_user):
    """근무 배정에 필요한 shift/position/checklist 데이터를 생성합니다."""
    store_id = str(store.id)
    admin_url = "/api/v1/admin"

    # 시간대 생성
    shift_res = await client.post(
        f"{admin_url}/stores/{store_id}/shifts",
        json={"name": "오전", "sort_order": 1},
        headers=auth_header(admin_token),
    )
    shift = shift_res.json()

    # 포지션 생성
    pos_res = await client.post(
        f"{admin_url}/stores/{store_id}/positions",
        json={"name": "그릴", "sort_order": 1},
        headers=auth_header(admin_token),
    )
    position = pos_res.json()

    # 체크리스트 템플릿 + 항목 생성
    tmpl_res = await client.post(
        f"{admin_url}/stores/{store_id}/checklist-templates",
        json={
            "shift_id": shift["id"],
            "position_id": position["id"],
            "title": "오전 그릴 체크리스트",
        },
        headers=auth_header(admin_token),
    )
    template = tmpl_res.json()

    for i, title in enumerate(["예열", "재료 준비", "위생 점검"]):
        await client.post(
            f"{admin_url}/checklist-templates/{template['id']}/items",
            json={"title": title, "sort_order": i},
            headers=auth_header(admin_token),
        )

    return {
        "store_id": store_id,
        "shift_id": shift["id"],
        "position_id": position["id"],
        "template_id": template["id"],
        "user_id": str(staff_user.id),
    }


class TestAssignmentCreate:
    """근무 배정 생성 테스트."""

    async def test_create_assignment(self, client: AsyncClient, admin_token, work_setup):
        """근무 배정 생성 성공 — 체크리스트 스냅샷 포함."""
        res = await client.post(URL, json={
            "store_id": work_setup["store_id"],
            "shift_id": work_setup["shift_id"],
            "position_id": work_setup["position_id"],
            "user_id": work_setup["user_id"],
            "work_date": str(date.today()),
        }, headers=auth_header(admin_token))
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["status"] == "assigned"
        assert data["total_items"] == 3
        assert data["completed_items"] == 0

    async def test_create_duplicate_assignment(self, client: AsyncClient, admin_token, work_setup):
        """동일 조합+날짜 중복 배정 실패."""
        payload = {
            "store_id": work_setup["store_id"],
            "shift_id": work_setup["shift_id"],
            "position_id": work_setup["position_id"],
            "user_id": work_setup["user_id"],
            "work_date": "2030-01-01",
        }
        await client.post(URL, json=payload, headers=auth_header(admin_token))
        res = await client.post(URL, json=payload, headers=auth_header(admin_token))
        assert res.status_code in (409, 400, 500)


class TestAssignmentRead:
    """근무 배정 조회 테스트."""

    async def test_list_assignments(self, client: AsyncClient, admin_token, work_setup):
        """근무 배정 목록 조회."""
        await client.post(URL, json={
            "store_id": work_setup["store_id"],
            "shift_id": work_setup["shift_id"],
            "position_id": work_setup["position_id"],
            "user_id": work_setup["user_id"],
            "work_date": "2030-06-15",
        }, headers=auth_header(admin_token))

        res = await client.get(URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert data["total"] >= 1

    async def test_get_assignment_detail_with_snapshot(self, client: AsyncClient, admin_token, work_setup):
        """근무 배정 상세 조회 — 체크리스트 스냅샷 확인."""
        create_res = await client.post(URL, json={
            "store_id": work_setup["store_id"],
            "shift_id": work_setup["shift_id"],
            "position_id": work_setup["position_id"],
            "user_id": work_setup["user_id"],
            "work_date": "2030-07-01",
        }, headers=auth_header(admin_token))
        assignment_id = create_res.json()["id"]

        res = await client.get(f"{URL}{assignment_id}", headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["checklist_snapshot"] is not None
        assert len(data["checklist_snapshot"]) == 3
        assert data["checklist_snapshot"][0]["title"] == "예열"
        assert data["checklist_snapshot"][0]["is_completed"] is False


class TestAssignmentDelete:
    """근무 배정 삭제 테스트."""

    async def test_delete_assignment(self, client: AsyncClient, admin_token, work_setup):
        """근무 배정 삭제."""
        create_res = await client.post(URL, json={
            "store_id": work_setup["store_id"],
            "shift_id": work_setup["shift_id"],
            "position_id": work_setup["position_id"],
            "user_id": work_setup["user_id"],
            "work_date": "2030-12-25",
        }, headers=auth_header(admin_token))
        assignment_id = create_res.json()["id"]

        res = await client.delete(f"{URL}{assignment_id}", headers=auth_header(admin_token))
        assert res.status_code == 200
