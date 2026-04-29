"""App Version 엔드포인트 테스트.

- POST /api/v1/admin/app-versions  (owner 권한)
- GET  /api/v1/admin/app-versions
- GET  /api/v1/attendance/app-version  (device token)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from app.database import async_session


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_app_versions():
    """각 테스트 전후 app_versions 비움 — DB 격리."""
    async with async_session() as db:
        await db.execute(text("DELETE FROM app_versions"))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(text("DELETE FROM app_versions"))
        await db.commit()


async def test_admin_create_then_list(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    payload = {
        "channel": "attendance_isolated_test_channel",
        "version": "1.0.0",
        "s3_key": "app-releases/attendance/v1.0.0/tma.apk",
        "is_latest": True,
        "is_min_required": False,
        "release_notes": "first",
    }
    resp = await async_client.post(
        "/api/v1/admin/app-versions", json=payload, headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["channel"] == "attendance_isolated_test_channel"
    assert body["is_latest"] is True

    # second release with is_latest=True should demote first
    payload2 = {**payload, "version": "1.0.1", "s3_key": "app-releases/attendance/v1.0.1/tma.apk"}
    resp2 = await async_client.post(
        "/api/v1/admin/app-versions", json=payload2, headers=admin_headers
    )
    assert resp2.status_code == 201
    list_resp = await async_client.get(
        "/api/v1/admin/app-versions?channel=attendance_isolated_test_channel", headers=admin_headers
    )
    assert list_resp.status_code == 200
    rows = list_resp.json()
    by_version = {r["version"]: r for r in rows}
    assert by_version["1.0.0"]["is_latest"] is False
    assert by_version["1.0.1"]["is_latest"] is True


async def test_attendance_app_version_no_release(
    async_client: AsyncClient,
    device_token: str,
) -> None:
    """채널에 등록된 릴리스가 없으면 모든 필드 None."""
    resp = await async_client.get(
        "/api/v1/attendance/app-version",
        headers={"Authorization": f"Bearer {device_token}"},
    )
    # 등록된 릴리스가 없는 채널 (attendance_<APP_ENV>) 일 때 모두 null 응답
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_version"] is None
    assert body["latest_version"] is None
    assert body["download_url"] is None
