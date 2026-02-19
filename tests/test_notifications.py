"""알림 API 테스트.

Notification API tests — List and mark as read.
Tests both admin and app notification endpoints.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import auth_header


ADMIN_NOTIFY_URL = "/api/v1/admin/notifications/"
APP_NOTIFY_URL = "/api/v1/app/my/notifications/"


@pytest_asyncio.fixture
async def notifications(db: AsyncSession, org, admin_user):
    """테스트용 알림 데이터를 생성합니다."""
    from app.models.notification import Notification
    notifs = []
    for i in range(3):
        n = Notification(
            organization_id=org.id,
            user_id=admin_user.id,
            type="announcement",
            message=f"Test notification {i}",
            is_read=False,
        )
        db.add(n)
        notifs.append(n)
    await db.flush()
    for n in notifs:
        await db.refresh(n)
    return notifs


class TestAdminNotifications:
    """관리자 알림 API 테스트."""

    async def test_list_notifications(self, client: AsyncClient, admin_token, notifications):
        """알림 목록 조회."""
        res = await client.get(ADMIN_NOTIFY_URL, headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        # 페이지네이션 응답
        if isinstance(data, dict) and "items" in data:
            assert data["total"] >= 3
        elif isinstance(data, list):
            assert len(data) >= 3

    async def test_mark_notification_read(self, client: AsyncClient, admin_token, notifications):
        """알림 읽음 처리."""
        notif_id = str(notifications[0].id)
        # PATCH /notifications/{id}/read 경로
        res = await client.patch(
            f"/api/v1/admin/notifications/{notif_id}/read",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200

    async def test_unread_count(self, client: AsyncClient, admin_token, notifications):
        """미읽음 알림 수 조회."""
        res = await client.get(
            "/api/v1/admin/notifications/unread-count",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200

    async def test_mark_all_read(self, client: AsyncClient, admin_token, notifications):
        """전체 알림 읽음 처리."""
        res = await client.patch(
            "/api/v1/admin/notifications/read-all",
            headers=auth_header(admin_token),
        )
        assert res.status_code == 200

    async def test_notifications_no_auth(self, client: AsyncClient):
        """인증 없이 알림 조회 시 403."""
        res = await client.get(ADMIN_NOTIFY_URL)
        assert res.status_code == 403


class TestAppNotifications:
    """앱(직원) 알림 API 테스트."""

    async def test_app_list_notifications(self, client: AsyncClient, admin_token, notifications):
        """직원 앱에서 내 알림 목록 조회."""
        res = await client.get(APP_NOTIFY_URL, headers=auth_header(admin_token))
        assert res.status_code == 200
