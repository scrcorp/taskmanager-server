"""신청 계열 생성 경로의 datetime 인코딩 불변식 매트릭스.

schedules 행을 만드는 6개 지점 중 request 계열 4곳(앱 신청/배치·admin 신청·복사)이
어떤 페이로드에서도 인코딩 불변식을 지키는지 API 레벨로 고정한다:
  ① start_time≠NULL ⇒ start_at≠NULL  ② operating_day≠NULL
  ③ start_at 날짜 − operating_day ∈ {0,1}  ④ start_at::time == start_time
  ⑤ end_at > start_at  ⑥ net == (end−start−break) 분
(콘솔 create/워크인은 test_schedule_datetime_create.py·test_early_morning_lifecycle.py가 커버.)
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

WEEK_FROM = date(2026, 11, 22)  # Sun
WEEK_TO = date(2026, 11, 28)    # Sat
PREV_FROM = WEEK_FROM - timedelta(days=7)
ALL_DATES = [PREV_FROM + timedelta(days=i) for i in range(21)]


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
async def staff_assigned(test_user, test_store_id) -> AsyncIterator[dict]:
    async with async_session() as db:
        exists = (await db.execute(select(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id
        ))).scalar_one_or_none()
        if exists is None:
            db.add(UserStore(user_id=test_user["id"], store_id=test_store_id,
                             is_work_assignment=True))
            await db.commit()
    yield {**test_user, "store_id": test_store_id}
    async with async_session() as db:
        await db.execute(delete(Attendance).where(
            Attendance.user_id == test_user["id"], Attendance.work_date.in_(ALL_DATES)))
        await db.execute(delete(Schedule).where(
            Schedule.user_id == test_user["id"], Schedule.operating_day.in_(ALL_DATES)))
        await db.commit()


async def _fetch(sched_id: str) -> Schedule:
    async with async_session() as db:
        return (await db.execute(
            select(Schedule).where(Schedule.id == UUID(sched_id))
        )).scalar_one()


def _assert_invariants(row: Schedule) -> None:
    assert row.operating_day is not None, "② operating_day NULL"
    if row.start_time is not None:
        assert row.start_at is not None, "① start_time 있는데 start_at NULL"
        assert row.start_at.time().replace(second=0) == row.start_time, "④ 시각 동기화 깨짐"
        delta = (row.start_at.date() - row.operating_day).days
        assert delta in (0, 1), f"③ 발산 위반: {delta}"
    if row.end_time is not None:
        assert row.end_at is not None, "① end_time 있는데 end_at NULL"
    if row.start_at is not None and row.end_at is not None:
        assert row.end_at > row.start_at, "⑤ end_at ≤ start_at"
        gross = int((row.end_at - row.start_at).total_seconds() // 60)
        brk = 0
        if row.break_start_at and row.break_end_at:
            brk = int((row.break_end_at - row.break_start_at).total_seconds() // 60)
        assert row.net_work_minutes == max(gross - brk, 0), "⑥ net 불일치"


async def test_app_request_overnight_encoding(async_client, staff_headers, staff_assigned):
    """앱 신청(create_request) — 자정넘김 preferred(22:00~02:00)."""
    resp = await async_client.post(
        "/api/v1/app/my/schedule-requests",
        headers=staff_headers,
        json={"store_id": str(staff_assigned["store_id"]),
              "work_date": WEEK_FROM.isoformat(),
              "preferred_start_time": "22:00", "preferred_end_time": "02:00"},
    )
    assert resp.status_code == 201, resp.text
    row = await _fetch(resp.json()["id"])
    _assert_invariants(row)
    assert row.start_at == datetime(2026, 11, 22, 22, 0)
    assert row.end_at == datetime(2026, 11, 23, 2, 0)   # 익일
    assert row.net_work_minutes == 240


async def test_app_request_timeless_is_legit_null(async_client, staff_headers, staff_assigned):
    """앱 신청 — 시각 없는 신청은 start_at NULL이 정당, 라벨은 반드시 있음."""
    resp = await async_client.post(
        "/api/v1/app/my/schedule-requests",
        headers=staff_headers,
        json={"store_id": str(staff_assigned["store_id"]),
              "work_date": (WEEK_FROM + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 201, resp.text
    row = await _fetch(resp.json()["id"])
    assert row.start_time is None and row.start_at is None
    assert row.operating_day == WEEK_FROM + timedelta(days=1)


async def test_admin_request_overnight_encoding(async_client, admin_headers, staff_assigned):
    """콘솔 admin 신청(admin_create_request) — 자정넘김."""
    resp = await async_client.post(
        "/api/v1/console/schedule-requests",
        headers=admin_headers,
        json={"store_id": str(staff_assigned["store_id"]),
              "user_id": str(staff_assigned["id"]),
              "work_date": (WEEK_FROM + timedelta(days=2)).isoformat(),
              "preferred_start_time": "23:00", "preferred_end_time": "07:00"},
    )
    assert resp.status_code == 200, resp.text  # admin create 엔드포인트는 200 반환
    row = await _fetch(resp.json()["id"])
    _assert_invariants(row)
    assert row.start_at.date() == WEEK_FROM + timedelta(days=2)
    assert row.end_at.date() == WEEK_FROM + timedelta(days=3)
    assert row.net_work_minutes == 480


async def test_copy_last_period_preserves_dawn_offset(async_client, staff_headers, staff_assigned):
    """복사(copy_last_period) — 지난주 새벽근무(+1d)를 복사해도 오프셋 보존."""
    prev_day = PREV_FROM + timedelta(days=3)
    async with async_session() as db:
        db.add(Schedule(
            organization_id=staff_assigned["organization_id"],
            user_id=staff_assigned["id"], store_id=staff_assigned["store_id"],
            operating_day=prev_day,
            start_at=datetime.combine(prev_day + timedelta(days=1), time(1, 0)),
            end_at=datetime.combine(prev_day + timedelta(days=1), time(9, 0)),
            net_work_minutes=480, status="requested",
        ))
        await db.commit()

    resp = await async_client.post(
        "/api/v1/app/my/schedule-requests/copy-last-period",
        headers=staff_headers,
        json={"store_id": str(staff_assigned["store_id"]),
              "date_from": WEEK_FROM.isoformat(), "date_to": WEEK_TO.isoformat()},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["created"]
    assert len(created) == 1, resp.json()
    row = await _fetch(created[0]["id"])
    _assert_invariants(row)
    new_day = prev_day + timedelta(days=7)
    assert row.operating_day == new_day
    assert row.start_at.date() == new_day + timedelta(days=1), "복사 시 +1d 평탄화됨"


async def test_copy_replace_keeps_break_consistent(async_client, staff_headers, staff_assigned):
    """copy on_conflict=replace — break를 무시한 net + 낡은 break_at 방치(불변식 ⑥)가 없어야 함."""
    prev_day = PREV_FROM + timedelta(days=4)
    new_day = prev_day + timedelta(days=7)
    async with async_session() as db:
        # 지난주: 브레이크 있는 신청 (09~17, break 12~13 → net 420)
        db.add(Schedule(
            organization_id=staff_assigned["organization_id"],
            user_id=staff_assigned["id"], store_id=staff_assigned["store_id"],
            operating_day=prev_day,
            start_at=datetime.combine(prev_day, time(9, 0)),
            end_at=datetime.combine(prev_day, time(17, 0)),
            break_start_at=datetime.combine(prev_day, time(12, 0)),
            break_end_at=datetime.combine(prev_day, time(13, 0)),
            net_work_minutes=420, status="requested",
        ))
        # 이번주 같은 날짜에 기존 requested(브레이크 없음, 다른 시간) — replace 대상
        db.add(Schedule(
            organization_id=staff_assigned["organization_id"],
            user_id=staff_assigned["id"], store_id=staff_assigned["store_id"],
            operating_day=new_day,
            start_at=datetime.combine(new_day, time(10, 0)),
            end_at=datetime.combine(new_day, time(15, 0)),
            net_work_minutes=300, status="requested",
        ))
        await db.commit()

    resp = await async_client.post(
        "/api/v1/app/my/schedule-requests/copy-last-period",
        headers=staff_headers,
        json={"store_id": str(staff_assigned["store_id"]),
              "date_from": WEEK_FROM.isoformat(), "date_to": WEEK_TO.isoformat(),
              "on_conflict": "replace"},
    )
    assert resp.status_code == 201, resp.text
    replaced = resp.json()["replaced"]
    assert len(replaced) == 1, resp.json()
    row = await _fetch(replaced[0]["id"])
    _assert_invariants(row)
    # prev의 브레이크가 양 인코딩으로 복사되고 net에 반영돼야 함
    assert row.break_start_at == datetime.combine(new_day, time(12, 0))
    assert row.break_end_at == datetime.combine(new_day, time(13, 0))
    assert row.break_start_time == time(12, 0)
    assert row.net_work_minutes == 420
