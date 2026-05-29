"""Integration tests — Issue 8 (한 직원 다중 schedule).

identify-by-pin 이 한 직원의 오늘 모든 attendance 를 today_attendances 로 반환하고
우선순위로 정렬하는지, clock-in 이 schedule_id 지정 시 그 schedule 에 매칭하는지.

기존엔 identify 가 .limit(1) 로 한 row 만 반환해 오전 clocked_out row 가 먼저 와서
오후 schedule 출근이 막히던 버그 회귀 방지.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session


pytestmark = pytest.mark.asyncio


async def _tz_for(store_id: UUID):
    from app.utils.timezone import get_store_day_config
    from zoneinfo import ZoneInfo
    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, store_id)
    return ZoneInfo(tz_name)


async def _ensure_attendance(schedule_id: UUID) -> None:
    from app.models.schedule import Schedule
    from sqlalchemy import select as sa_select
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule
    async with async_session() as db:
        sched = (await db.execute(sa_select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()


async def _set_attendance(schedule_id: UUID, *, clock_in, status: str) -> None:
    async with async_session() as db:
        await db.execute(
            text("UPDATE attendances SET clock_in = :ci, status = :s WHERE schedule_id = :sid"),
            {"ci": clock_in, "s": status, "sid": schedule_id},
        )
        await db.commit()


async def test_identify_returns_all_today_attendances_sorted(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """오전 clocked_out + 오후 upcoming 두 schedule → today_attendances 2건, upcoming 우선."""
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)

    # 오전 (이미 끝남): 4시간 전 시작, 1시간 전 종료
    morning_start = (now_local - timedelta(hours=4)).time().replace(microsecond=0)
    morning_end = (now_local - timedelta(hours=1)).time().replace(microsecond=0)
    morning = await make_schedule(test_user, start_time=morning_start, end_time=morning_end)
    await _ensure_attendance(morning)
    await _set_attendance(
        morning,
        clock_in=datetime.now(timezone.utc) - timedelta(hours=4),
        status="clocked_out",
    )

    # 오후 (곧 시작): 1시간 후 시작
    aft_start = (now_local + timedelta(hours=1)).time().replace(microsecond=0)
    aft_end = (now_local + timedelta(hours=5)).time().replace(microsecond=0)
    afternoon = await make_schedule(test_user, start_time=aft_start, end_time=aft_end)
    await _ensure_attendance(afternoon)
    await _set_attendance(afternoon, clock_in=None, status="upcoming")

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    items = body["today_attendances"]
    assert len(items) == 2, f"두 schedule 모두 반환해야 함: {items}"
    # 우선순위: upcoming(4) < clocked_out(6) → upcoming 이 primary
    assert items[0]["status"] == "upcoming"
    assert items[1]["status"] == "clocked_out"
    # primary 호환 필드
    assert body["today_status"] == "upcoming"
    # schedule_id 포함
    sched_ids = {it["schedule_id"] for it in items}
    assert str(afternoon) in sched_ids
    assert str(morning) in sched_ids


async def test_clock_in_with_schedule_id_matches_that_schedule(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """schedule_id 지정 clock-in → 그 schedule 의 attendance 에 기록."""
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)

    # 둘 다 지금 시작 (early threshold 안). s1 이 우선순위상 먼저지만 s2 를 명시 선택.
    s1_start = now_local.time().replace(microsecond=0)
    s1 = await make_schedule(
        test_user, start_time=s1_start,
        end_time=(now_local + timedelta(hours=2)).time().replace(microsecond=0),
    )
    await _ensure_attendance(s1)
    # s2 도 지금 시작 (1분 차이 — early guard 안 걸림). end 만 더 늦게.
    s2_start = now_local.time().replace(microsecond=0)
    s2 = await make_schedule(
        test_user, start_time=s2_start,
        end_time=(now_local + timedelta(hours=6)).time().replace(microsecond=0),
    )
    await _ensure_attendance(s2)

    # s2 (먼 미래) 를 명시적으로 선택해 clock-in
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "schedule_id": str(s2),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 응답의 schedule_id 가 s2 여야 함 (우선순위로는 s1 이 선택됐을 것)
    assert body["schedule_id"] == str(s2), (
        f"명시 선택한 s2 가 아닌 다른 schedule 에 기록됨: {body['schedule_id']}"
    )


async def test_clock_in_with_clocked_out_schedule_id_rejected(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """이미 clocked_out 된 schedule_id 명시 clock-in → 400 거부."""
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)

    done = await make_schedule(
        test_user,
        start_time=(now_local - timedelta(hours=4)).time().replace(microsecond=0),
        end_time=(now_local - timedelta(hours=1)).time().replace(microsecond=0),
    )
    await _ensure_attendance(done)
    await _set_attendance(
        done,
        clock_in=datetime.now(timezone.utc) - timedelta(hours=4),
        status="clocked_out",
    )
    # 두 번째 활성 schedule (clock-in 가능)
    live = await make_schedule(
        test_user, start_time=now_local.time().replace(microsecond=0),
        end_time=(now_local + timedelta(hours=4)).time().replace(microsecond=0),
    )
    await _ensure_attendance(live)

    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "schedule_id": str(done),  # 이미 끝난 shift 명시
        },
    )
    assert resp.status_code == 400, resp.text
    assert "not available" in resp.json()["detail"].lower()
