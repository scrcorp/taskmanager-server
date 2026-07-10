"""API integration — /app/schedules 가 datetime 인코딩(start_at/end_at/operating_day)을 방출.

Wave 2 앱 소비 기반: staff 앱 모델이 start_at을 파싱하려면 서버 앱-facing 응답이
그 필드를 실어야 한다. 자정 이후(새벽) 근무가 실제 날짜로 방출되는지 검증.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.main import app
from app.models.schedule import Schedule

pytestmark = pytest.mark.asyncio

WD = date(2026, 12, 22)  # 영업일


@pytest_asyncio.fixture
async def staff_headers() -> dict[str, str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/app/auth/login",
            json={"username": "teststaff", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def early_morning_schedule(test_user, test_store_id) -> AsyncIterator[str]:
    """영업일 12/22, 실제 근무 12/23 01:00~09:00 스케줄(datetime 인코딩 저장)."""
    async with async_session() as db:
        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=test_store_id,
            work_date=WD,
            operating_day=WD,
            start_time=time(1, 0),
            end_time=time(9, 0),
            start_at=datetime(2026, 12, 23, 1, 0),
            end_at=datetime(2026, 12, 23, 9, 0),
            net_work_minutes=480,
            status="confirmed",
        )
        db.add(sched)
        await db.commit()
        sid = str(sched.id)
    try:
        yield sid
    finally:
        async with async_session() as db:
            await db.execute(delete(Schedule).where(Schedule.work_date == WD))
            await db.commit()


async def test_my_schedules_emits_datetime_encoding(
    async_client, staff_headers, early_morning_schedule
):
    resp = await async_client.get(
        f"/api/v1/app/my/schedules?date_from={WD.isoformat()}&date_to={WD.isoformat()}",
        headers=staff_headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    it = items[0]
    # 신 필드가 방출되고 실제 시각(12/23)을 실어야 함 — 영업일 라벨은 12/22
    assert it["operating_day"] == "2026-12-22"
    assert it["start_at"] == "2026-12-23T01:00"
    assert it["end_at"] == "2026-12-23T09:00"
    # 구 필드도 하위호환으로 함께 방출
    assert it["start_time"] == "01:00"
    assert it["work_date"] == "2026-12-22"


async def test_my_schedule_detail_emits_datetime_encoding(
    async_client, staff_headers, early_morning_schedule
):
    resp = await async_client.get(
        f"/api/v1/app/my/schedules/{early_morning_schedule}", headers=staff_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_at"] == "2026-12-23T01:00"
    assert body["operating_day"] == "2026-12-22"
