"""API integration — Windowed Roster 엔드포인트 (GET /api/v1/console/schedules/roster).

검증:
- week granularity: TEAM=스케줄 수, 날짜별 컬럼, roster 행/요약
- day granularity: 30분 점유 0.5 환산
- schedule-level 필터(position)로 비매칭 시 빈 결과
- cost 마스킹 (SV 이하)
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

FUTURE = date(2030, 1, 2)  # 다른 테스트와 겹치지 않는 고정일
ROSTER_URL = "/api/v1/console/schedules/roster"


@pytest_asyncio.fixture
async def staff_in_test_store(test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 test_store 에 is_work_assignment=True 로 배정 (roster 후보 포함 조건)."""
    async with async_session() as db:
        existing = await db.execute(
            select(UserStore).where(
                UserStore.user_id == test_user["id"],
                UserStore.store_id == test_store_id,
            )
        )
        row = existing.scalar_one_or_none()
        created = False
        if row is None:
            db.add(UserStore(
                user_id=test_user["id"], store_id=test_store_id,
                is_manager=False, is_work_assignment=True,
            ))
            created = True
        else:
            row.is_work_assignment = True
        await db.commit()
    try:
        yield test_user
    finally:
        if created:
            async with async_session() as db:
                await db.execute(delete(UserStore).where(
                    UserStore.user_id == test_user["id"],
                    UserStore.store_id == test_store_id,
                ))
                await db.commit()


@pytest_asyncio.fixture
async def _clear_future(test_store_id) -> AsyncIterator[None]:
    """FUTURE 날짜 스케줄을 테스트 전후로 정리 (격리)."""
    async def _wipe():
        async with async_session() as db:
            await db.execute(delete(Schedule).where(
                Schedule.store_id == test_store_id, Schedule.operating_day == FUTURE,
            ))
            await db.commit()
    await _wipe()
    yield
    await _wipe()


@pytest_asyncio.fixture
async def sv_in_test_store(test_users, test_store_id) -> AsyncIterator[dict]:
    """testsv 를 test_store 에 배정 (store 접근권 확보)."""
    sv = test_users["testsv"]
    async with async_session() as db:
        existing = await db.execute(select(UserStore).where(
            UserStore.user_id == sv["id"], UserStore.store_id == test_store_id,
        ))
        created = existing.scalar_one_or_none() is None
        if created:
            db.add(UserStore(
                user_id=sv["id"], store_id=test_store_id,
                is_manager=True, is_work_assignment=True,
            ))
            await db.commit()
    try:
        yield sv
    finally:
        if created:
            async with async_session() as db:
                await db.execute(delete(UserStore).where(
                    UserStore.user_id == sv["id"], UserStore.store_id == test_store_id,
                ))
                await db.commit()


@pytest_asyncio.fixture
async def sv_headers(test_users) -> dict[str, str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": "testsv", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _insert(store_id, user_info, *, status="confirmed", start=time(9, 0), end=time(17, 0),
                  position=None, work_role_name=None, hourly_rate=None, net_work_minutes=0,
                  start_at=None, end_at=None, operating_day=None):
    # Wave 3: 구 컬럼 제거 — start/end(HH:MM) 는 operating_day 기준 datetime 으로 조립.
    op_day = operating_day or FUTURE
    if start_at is None and start is not None:
        start_at = datetime.combine(op_day, start)
    if end_at is None and end is not None:
        end_at = datetime.combine(op_day, end)
        if start_at is not None and end_at <= start_at:
            end_at += timedelta(days=1)
    async with async_session() as db:
        s = Schedule(
            organization_id=user_info["organization_id"], user_id=user_info["id"],
            store_id=store_id, operating_day=op_day,
            status=status, position_snapshot=position, work_role_name_snapshot=work_role_name,
            net_work_minutes=net_work_minutes,
            start_at=start_at, end_at=end_at,
        )
        if hourly_rate is not None:
            s.hourly_rate = hourly_rate
        db.add(s)
        await db.commit()


async def test_roster_week_team_is_schedule_count(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # 같은 사람 2 스케줄 → TEAM = 2 (고유 인원 아님), staff_count = 1
    await _insert(test_store_id, staff_in_test_store, start=time(9, 0), end=time(12, 0))
    await _insert(test_store_id, staff_in_test_store, start=time(14, 0), end=time(17, 0))

    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["team_confirmed"] == 2
    assert body["totals"]["staff_count"] == 1
    col = next(c for c in body["columns"] if c["key"] == FUTURE.isoformat())
    assert col["team_confirmed"] == 2
    row = next(r for r in body["roster"] if r["user_id"] == str(staff_in_test_store["id"]))
    assert row["has_schedule_in_period"] is True


async def test_roster_day_half_hour_is_half_person(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # 9:00~9:30 → h9 슬롯 0.5인
    await _insert(test_store_id, staff_in_test_store, start=time(9, 0), end=time(9, 30))

    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "day", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    cols = {c["key"]: c for c in resp.json()["columns"]}
    assert cols["h9"]["team_confirmed"] == 0.5


async def test_roster_day_slots_half_hour_first_only(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # 9:00~9:30 confirmed 1건 → h9 첫 30분만 인원. slots=[1,0], team(점유합)=0.5
    await _insert(test_store_id, staff_in_test_store, start=time(9, 0), end=time(9, 30),
                  net_work_minutes=30)
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "day", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    cols = {c["key"]: c for c in resp.json()["columns"]}
    assert cols["h9"]["slots_confirmed"] == [1, 0]
    assert cols["h9"]["slots_pending"] == [0, 0]
    assert cols["h9"]["team_confirmed"] == 0.5


async def test_roster_day_slots_full_hour_both(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # 9:00~10:00 confirmed 1건 → 두 슬롯 모두 인원. slots=[1,1], team(점유합)=1.0
    await _insert(test_store_id, staff_in_test_store, start=time(9, 0), end=time(10, 0),
                  net_work_minutes=60)
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "day", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    cols = {c["key"]: c for c in resp.json()["columns"]}
    assert cols["h9"]["slots_confirmed"] == [1, 1]
    assert cols["h9"]["team_confirmed"] == 1.0


async def test_roster_week_slots_empty(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # week granularity 에서는 slots 빈 배열 유지
    await _insert(test_store_id, staff_in_test_store, start=time(9, 0), end=time(10, 0),
                  net_work_minutes=60)
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    col = next(c for c in resp.json()["columns"] if c["key"] == FUTURE.isoformat())
    assert col["slots_confirmed"] == []
    assert col["slots_pending"] == []


async def test_roster_position_filter_excludes_nonmatching(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    await _insert(test_store_id, staff_in_test_store, position="Kitchen")

    # 존재하지 않는 position 필터 → 빈 결과 + filter_domain 엔 Kitchen
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id), "positions": "Cashier",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["team_confirmed"] == 0
    assert body["roster"] == []  # schedule-level 필터 활성 → 매칭 없는 행 숨김
    assert "Kitchen" in body["filter_domain"]["positions"]


async def test_roster_cost_computed_with_decimal_rate(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    # hourly_rate 가 DB Numeric(Decimal)로 저장되어도 cost 계산 시 500 안 나야 함 (float 캐스팅).
    await _insert(test_store_id, staff_in_test_store, hourly_rate=20, net_work_minutes=480)
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["cost_confirmed"] == 160.0  # 8h * $20
    assert body["totals"]["hours_confirmed"] == 8.0


async def test_roster_cost_masked_for_sv(
    async_client, sv_headers, sv_in_test_store, staff_in_test_store, test_store_id, _clear_future
):
    await _insert(test_store_id, staff_in_test_store)
    resp = await async_client.get(ROSTER_URL, headers=sv_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["cost_confirmed"] is None
    for r in body["roster"]:
        assert r["confirmed_cost"] is None
        assert r["effective_hourly_rate"] is None


async def test_roster_day_plus1d_dawn_placed_on_operating_axis(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    """+1d 새벽 근무(start_at 날짜=영업일+1)는 h25.. 컬럼(1A+1)에 배치되고
    h1..(당일 아침)에는 배치되지 않아야 한다 — 물리 위치 오배치 회귀 방지."""
    from datetime import datetime as _dt
    await _insert(
        test_store_id, staff_in_test_store,
        start=time(1, 0), end=time(9, 0),
        operating_day=FUTURE,
        start_at=_dt.combine(FUTURE, time(1, 0)) + timedelta(days=1),
        end_at=_dt.combine(FUTURE, time(9, 0)) + timedelta(days=1),
    )
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "day", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    cols = {c["key"]: c for c in resp.json()["columns"]}
    # 영업일 축 25..32 에 1인 배치
    for h in range(25, 33):
        assert cols[f"h{h}"]["team_confirmed"] == 1, f"h{h} 비어있음"
    # 당일 아침 h1..h8 에는 컬럼 자체가 없거나 0 (h_lo 가 25부터 시작)
    for h in range(1, 9):
        assert f"h{h}" not in cols or cols[f"h{h}"]["team_confirmed"] == 0


async def test_roster_day_plus1d_within_range_dawn_half_hour(
    async_client, admin_headers, staff_in_test_store, test_store_id, _clear_future
):
    """+1d 00:30~02:00 → h24=0.5, h25=1 (30분 경계 + 오프셋 동시)."""
    from datetime import datetime as _dt
    await _insert(
        test_store_id, staff_in_test_store,
        start=time(0, 30), end=time(2, 0),
        operating_day=FUTURE,
        start_at=_dt.combine(FUTURE, time(0, 30)) + timedelta(days=1),
        end_at=_dt.combine(FUTURE, time(2, 0)) + timedelta(days=1),
    )
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat(),
        "granularity": "day", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    cols = {c["key"]: c for c in resp.json()["columns"]}
    assert cols["h24"]["team_confirmed"] == 0.5
    assert cols["h25"]["team_confirmed"] == 1
