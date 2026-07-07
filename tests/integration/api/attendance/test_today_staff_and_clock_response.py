"""Integration tests — Issue 3 (clock-in 후 반영 안 됨):

- today-staff 가 compute_effective_status 와 일관 (late → working 승격)
- clock 액션 응답에 effective_status 필드 포함
- effective_status 의 'late' 는 미출근 지각 한정 (출근 후엔 'working')

Phase 5 직후엔 today-staff 가 자체 inline effective_status 로 'late' 그대로 응답해
client 의 WORKING 사이드바/Schedule 'On Shift' 섹션에서 빠지는 버그가 있었음.
이 테스트가 회귀 방지.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session


pytestmark = pytest.mark.asyncio


async def _ensure_attendance(schedule_id: UUID) -> None:
    """make_schedule fixture 는 Schedule 만 직접 INSERT 라 attendance 가 없다.
    service 의 ensure_attendance_for_schedule 호출해 row 보장."""
    from app.models.schedule import Schedule
    from sqlalchemy import select as sa_select
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    async with async_session() as db:
        sched = (await db.execute(sa_select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()


async def _set_attendance_state(
    schedule_id: UUID,
    *,
    clock_in: datetime | None,
    status: str,
) -> None:
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE attendances SET clock_in = :ci, status = :s "
                "WHERE schedule_id = :sid"
            ),
            {"ci": clock_in, "s": status, "sid": schedule_id},
        )
        await db.commit()


async def _tz_for(store_id: UUID):
    from app.utils.timezone import get_store_day_config
    from zoneinfo import ZoneInfo
    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, store_id)
    return ZoneInfo(tz_name)


async def test_today_staff_late_attendance_returns_working_effective_status(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """clock_in 있는데 DB status='late' → today-staff 응답 status='working' 으로 승격.

    drift 회귀 방지: dashboard 의 inline effective_status 가 compute_effective_status 로
    교체된 후엔 late → working 자동 변환.
    """
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)
    start_local = (now_local - timedelta(minutes=30)).time().replace(microsecond=0)
    end_local = (now_local + timedelta(hours=4)).time().replace(microsecond=0)

    schedule_id = await make_schedule(
        test_user, start_time=start_local, end_time=end_local,
    )
    await _ensure_attendance(schedule_id)

    # attendance 를 'late' + clock_in 있음 상태로 강제
    clock_in_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    await _set_attendance_state(schedule_id, clock_in=clock_in_at, status="late")

    resp = await async_client.get(
        "/api/v1/attendance/today-staff",
        headers=device_auth_headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    target = next((r for r in rows if r["user_id"] == str(test_user["id"])), None)
    assert target is not None, f"user row 없음 — rows={rows}"
    # late → working 승격 (drift fix)
    assert target["status"] == "working", (
        f"late 상태인데 today-staff 응답 status가 'working' 으로 승격 안 됨: {target}"
    )


async def test_today_staff_etag_returns_304_when_unchanged(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """today-staff 는 ETag 를 주고, If-None-Match 로 변경 없으면 304(빈 바디)."""
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)
    start_local = (now_local - timedelta(minutes=30)).time().replace(microsecond=0)
    end_local = (now_local + timedelta(hours=4)).time().replace(microsecond=0)
    schedule_id = await make_schedule(
        test_user, start_time=start_local, end_time=end_local,
    )
    await _ensure_attendance(schedule_id)

    # 1) 첫 호출 — 200 + ETag 헤더
    first = await async_client.get(
        "/api/v1/attendance/today-staff", headers=device_auth_headers,
    )
    assert first.status_code == 200, first.text
    etag = first.headers.get("etag")
    assert etag, f"ETag 헤더 없음 — headers={dict(first.headers)}"

    # 2) 같은 ETag 로 재요청 — 변경 없으면 304 + 빈 바디
    second = await async_client.get(
        "/api/v1/attendance/today-staff",
        headers={**device_auth_headers, "If-None-Match": etag},
    )
    assert second.status_code == 304, second.text
    assert second.content in (b"", b"null"), second.content
    # 304 에도 동일 ETag 유지
    assert second.headers.get("etag") == etag


async def test_today_staff_no_clock_in_late_stays_late(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """clock_in 없는 'late' 는 그대로 'late' (미출근 지각)."""
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)
    # 1시간 전 시작 — late 영역
    start_local = (now_local - timedelta(hours=1)).time().replace(microsecond=0)
    end_local = (now_local + timedelta(hours=3)).time().replace(microsecond=0)

    schedule_id = await make_schedule(
        test_user, start_time=start_local, end_time=end_local,
    )
    await _ensure_attendance(schedule_id)
    await _set_attendance_state(schedule_id, clock_in=None, status="late")

    resp = await async_client.get(
        "/api/v1/attendance/today-staff",
        headers=device_auth_headers,
    )
    assert resp.status_code == 200
    rows = resp.json()
    target = next((r for r in rows if r["user_id"] == str(test_user["id"])), None)
    assert target is not None
    assert target["status"] == "late", (
        f"clock_in 없는데 late 가 잘못 승격됨: {target}"
    )


async def test_build_response_includes_effective_status(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """clock-in 액션 응답에 effective_status 키 포함 — client 의 응답 기반 patch 가 사용.

    Issue 3 트랙 A: refresh() 호출 없이 응답으로 dashboard row patch 하려면
    응답에 effective_status 가 있어야 함.
    """
    tz = await _tz_for(test_store_id)
    now_local = datetime.now(tz)
    # 지금 시작 — early threshold 안 걸리고 late 도 아님
    start_local = now_local.time().replace(microsecond=0)
    end_local = (now_local + timedelta(hours=4)).time().replace(microsecond=0)

    await make_schedule(
        test_user, start_time=start_local, end_time=end_local,
    )

    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "status" in body  # 기존 raw status 유지
    assert "effective_status" in body, (
        "build_response 응답에 effective_status 키 없음 — client 가 dashboard row 를 patch 할 때 사용"
    )
    assert body["effective_status"] in ("working", "late"), (
        f"clock-in 직후 effective_status: {body['effective_status']}"
    )
