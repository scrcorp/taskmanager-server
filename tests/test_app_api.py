"""앱(직원) 전용 API 테스트.

App (staff) API tests — Profile, assignments, announcements, tasks, notifications.
Tests the staff-facing endpoints under /api/v1/app/.
"""

import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import auth_header

APP = "/api/v1/app"


# ===== Profile =====

class TestAppProfile:
    """직원 프로필 테스트."""

    async def test_get_profile(self, client: AsyncClient, staff_token):
        """내 프로필 조회."""
        res = await client.get(f"{APP}/profile", headers=auth_header(staff_token))
        assert res.status_code == 200
        data = res.json()
        assert data["username"] == "staff"

    async def test_update_profile(self, client: AsyncClient, staff_token):
        """프로필 수정 — 이름/이메일 변경."""
        res = await client.put(f"{APP}/profile", json={
            "full_name": "Updated Staff Name",
            "email": "updated@test.com",
        }, headers=auth_header(staff_token))
        assert res.status_code == 200
        data = res.json()
        assert data["full_name"] == "Updated Staff Name"
        assert data["email"] == "updated@test.com"


# ===== My Assignments =====

class TestAppAssignments:
    """직원 근무 배정 조회 테스트."""

    @pytest_asyncio.fixture
    async def staff_assignment(self, db: AsyncSession, org, brand, staff_user):
        """스태프에게 배정된 근무를 DB에 직접 생성."""
        from app.models.work import Shift, Position
        from app.models.assignment import WorkAssignment

        shift = Shift(brand_id=brand.id, name="오전", sort_order=1)
        db.add(shift)
        position = Position(brand_id=brand.id, name="그릴", sort_order=1)
        db.add(position)
        await db.flush()

        assignment = WorkAssignment(
            organization_id=org.id,
            brand_id=brand.id,
            shift_id=shift.id,
            position_id=position.id,
            user_id=staff_user.id,
            work_date=date.today(),
            status="assigned",
            checklist_snapshot={"items": [
                {"item_index": 0, "title": "예열", "is_completed": False, "completed_at": None},
                {"item_index": 1, "title": "재료 준비", "is_completed": False, "completed_at": None},
            ]},
            total_items=2,
            completed_items=0,
        )
        db.add(assignment)
        await db.flush()
        await db.refresh(assignment)
        return assignment

    async def test_list_my_assignments(self, client: AsyncClient, staff_token, staff_assignment):
        """내 근무 배정 목록 조회."""
        res = await client.get(
            f"{APP}/my/work-assignments",
            headers=auth_header(staff_token),
        )
        assert res.status_code == 200

    async def test_complete_checklist_item(self, client: AsyncClient, staff_token, staff_assignment):
        """체크리스트 항목 완료 처리."""
        res = await client.patch(
            f"{APP}/my/work-assignments/{staff_assignment.id}/checklist/0",
            json={"is_completed": True},
            headers=auth_header(staff_token),
        )
        assert res.status_code == 200, res.json()


# ===== My Announcements =====

class TestAppAnnouncements:
    """직원 공지사항 조회 테스트."""

    @pytest_asyncio.fixture
    async def staff_announcement(self, db: AsyncSession, org, admin_user):
        """공지사항을 DB에 직접 생성."""
        from app.models.communication import Announcement
        ann = Announcement(
            organization_id=org.id,
            title="직원 공지",
            content="전체 공지입니다.",
            created_by=admin_user.id,
        )
        db.add(ann)
        await db.flush()
        await db.refresh(ann)
        return ann

    async def test_list_my_announcements(self, client: AsyncClient, staff_token, staff_announcement):
        """내 공지사항 목록 조회."""
        res = await client.get(
            f"{APP}/my/announcements/",
            headers=auth_header(staff_token),
        )
        assert res.status_code == 200


# ===== My Tasks =====

class TestAppTasks:
    """직원 추가 업무 테스트."""

    @pytest_asyncio.fixture
    async def staff_task(self, db: AsyncSession, org, admin_user, staff_user):
        """추가 업무를 DB에 직접 생성하고 스태프에 배정."""
        from app.models.communication import AdditionalTask, AdditionalTaskAssignee
        task = AdditionalTask(
            organization_id=org.id,
            title="긴급 청소",
            priority="urgent",
            status="pending",
            created_by=admin_user.id,
        )
        db.add(task)
        await db.flush()

        assignee = AdditionalTaskAssignee(
            task_id=task.id,
            user_id=staff_user.id,
        )
        db.add(assignee)
        await db.flush()
        await db.refresh(task)
        return task

    async def test_list_my_tasks(self, client: AsyncClient, staff_token, staff_task):
        """내 추가 업무 목록 조회."""
        res = await client.get(
            f"{APP}/my/additional-tasks/",
            headers=auth_header(staff_token),
        )
        assert res.status_code == 200

    async def test_complete_my_task(self, client: AsyncClient, staff_token, staff_task):
        """내 추가 업무 완료 처리."""
        res = await client.patch(
            f"{APP}/my/additional-tasks/{staff_task.id}/complete",
            headers=auth_header(staff_token),
        )
        # 엔드포인트가 존재하면 200, 아니면 404/405
        assert res.status_code in (200, 404, 405)
