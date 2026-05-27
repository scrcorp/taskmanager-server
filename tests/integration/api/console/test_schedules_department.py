"""API integration — ScheduleResponse 에 배정 직원의 department(FOH/BOH) 포함.

대상: GET /api/v1/console/schedules/{entry_id}  (schedule_service._to_response 경로)

스케줄 탭 필터는 콘솔이 클라이언트 사이드로 처리하므로, 서버는 각 스케줄에
배정된 직원의 department 를 실어주기만 하면 된다.

[작성됨]
- 직원 department="FOH" → 스케줄 응답 user_department == "FOH"
- 직원 미지정(None) → 스케줄 응답 user_department is None
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import update

from app.database import async_session
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def staff_with_foh(test_user) -> AsyncIterator[dict]:
    """test_user(teststaff) 의 department 를 'foh' 로 설정 → teardown 에서 None 복원."""
    async with async_session() as db:
        await db.execute(
            update(User).where(User.id == test_user["id"]).values(department="FOH")
        )
        await db.commit()
    try:
        yield test_user
    finally:
        async with async_session() as db:
            await db.execute(
                update(User).where(User.id == test_user["id"]).values(department=None)
            )
            await db.commit()


async def test_schedule_response_includes_user_department(
    async_client, admin_headers, make_schedule, staff_with_foh
):
    schedule_id = await make_schedule(staff_with_foh)

    resp = await async_client.get(
        f"/api/v1/console/schedules/{schedule_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(staff_with_foh["id"])
    assert body["user_department"] == "FOH"


async def test_schedule_response_department_null_when_unset(
    async_client, admin_headers, make_schedule, test_user
):
    """직원 department 미지정이면 스케줄 응답도 None."""
    schedule_id = await make_schedule(test_user)

    resp = await async_client.get(
        f"/api/v1/console/schedules/{schedule_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_department"] is None
