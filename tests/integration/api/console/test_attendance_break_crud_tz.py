"""API integration — admin break CRUD의 naive datetime 정규화 회귀 테스트.

시나리오 시딩 중 발견: naive started_at/ended_at 입력 시 기존 aware 세션과의
겹침 비교에서 TypeError → 500. 수정 후 naive 입력은 store 벽시계로 해석되어야
한다 (P0-5 interpret_clock_time 규칙과 동일).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.database import async_session
from app.utils.timezone import get_store_timezone

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _make_attendance(async_client, admin_headers, make_schedule, test_user, day_offset=-3):
    """과거 날짜 스케줄 + clock_in/out 정정으로 닫힌 attendance 하나 생성."""
    from datetime import date, time

    target = date.today() + timedelta(days=day_offset)
    sched_id = await make_schedule(
        test_user, work_date=target, start_time=time(9, 0), end_time=time(15, 0)
    )
    # make_schedule 픽스처는 DB 직삽입이라 attendance 행을 만들지 않는다
    from sqlalchemy import select as sa_select

    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    async with async_session() as db:
        sched = (await db.execute(sa_select(Schedule).where(Schedule.id == sched_id))).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()
    resp = await async_client.get(
        ATT_URL,
        headers=admin_headers,
        params={"user_id": test_user["id"], "work_date": target.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    att = next(it for it in resp.json()["items"] if it["schedule_id"] == str(sched_id))
    for field, hhmm in (("clock_in", "09:00"), ("clock_out", "15:00")):
        r = await async_client.patch(
            f"{ATT_URL}/{att['id']}/correct",
            headers=admin_headers,
            json={
                "field_name": field,
                "corrected_value": f"{target.isoformat()}T{hhmm}:00",
                "reason": "test setup",
            },
        )
        assert r.status_code == 200, r.text
    return att["id"], target


async def test_add_break_naive_input_interpreted_as_store_tz(
    async_client, admin_headers, make_schedule, test_user, test_store_id
):
    att_id, target = await _make_attendance(async_client, admin_headers, make_schedule, test_user)

    # naive 입력 → 500이 아니라 201, store 벽시계로 해석되어 저장
    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{target.isoformat()}T12:00:00",
            "ended_at": f"{target.isoformat()}T12:30:00",
            "break_type": "unpaid_meal",
        },
    )
    assert r.status_code in (200, 201), r.text

    async with async_session() as db:
        tz_name = await get_store_timezone(db, test_store_id)
    tz = ZoneInfo(tz_name)
    detail = (await async_client.get(f"{ATT_URL}/{att_id}", headers=admin_headers)).json()
    brk = detail["breaks"][0]
    stored = datetime.fromisoformat(brk["started_at"].replace("Z", "+00:00"))
    assert stored.astimezone(tz).strftime("%H:%M") == "12:00"
    assert brk["duration_minutes"] == 30

    # 두 번째 naive 세션 — 기존 aware 세션과의 겹침 비교가 TypeError 없이 동작 (회귀 핵심)
    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{target.isoformat()}T10:00:00",
            "ended_at": f"{target.isoformat()}T10:10:00",
            "break_type": "paid_10min",
        },
    )
    assert r.status_code in (200, 201), r.text

    # 실제 겹치는 naive 입력은 400 (500 아님)
    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{target.isoformat()}T12:15:00",
            "ended_at": f"{target.isoformat()}T12:45:00",
            "break_type": "unpaid_meal",
        },
    )
    assert r.status_code == 400, r.text


async def test_update_break_naive_input_interpreted_as_store_tz(
    async_client, admin_headers, make_schedule, test_user, test_store_id
):
    att_id, target = await _make_attendance(async_client, admin_headers, make_schedule, test_user, day_offset=-4)
    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{target.isoformat()}T12:00:00",
            "ended_at": f"{target.isoformat()}T12:30:00",
            "break_type": "unpaid_meal",
        },
    )
    assert r.status_code in (200, 201), r.text
    break_id = r.json()["id"]

    # naive 로 수정 → store 벽시계 해석, 500 없음
    r = await async_client.patch(
        f"{ATT_URL}/{att_id}/breaks/{break_id}",
        headers=admin_headers,
        json={"ended_at": f"{target.isoformat()}T12:40:00"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["duration_minutes"] == 40


async def test_correct_clock_out_without_clock_in_rejected(
    async_client, admin_headers, make_schedule, test_user, test_store_id
):
    """반쪽 펀치 방지 — clock_in 없는 근태에 clock_out 정정은 400.

    edge 테스트에서 발견: 아웃만 있는 기록은 근무시간 NULL 로 남아 어떤 게이트에도
    안 걸리고 조용히 0원이 된다. 정정 시점에 차단한다.
    """
    from datetime import date, time, timedelta

    target = date.today() - timedelta(days=5)
    sched_id = await make_schedule(
        test_user, work_date=target, start_time=time(9, 0), end_time=time(15, 0)
    )
    from sqlalchemy import select as sa_select

    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    async with async_session() as db:
        sched = (await db.execute(sa_select(Schedule).where(Schedule.id == sched_id))).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()
    resp = await async_client.get(
        ATT_URL, headers=admin_headers,
        params={"user_id": test_user["id"], "work_date": target.isoformat()},
    )
    att = next(it for it in resp.json()["items"] if it["schedule_id"] == str(sched_id))

    r = await async_client.patch(
        f"{ATT_URL}/{att['id']}/correct", headers=admin_headers,
        json={"field_name": "clock_out",
              "corrected_value": f"{target.isoformat()}T15:00:00",
              "reason": "half punch attempt"},
    )
    assert r.status_code == 400, r.text
    assert "clock-in" in r.json()["detail"].lower()

    # clock_in 먼저 → clock_out 순서는 정상 동작
    for field, hhmm in (("clock_in", "09:00"), ("clock_out", "15:00")):
        r = await async_client.patch(
            f"{ATT_URL}/{att['id']}/correct", headers=admin_headers,
            json={"field_name": field,
                  "corrected_value": f"{target.isoformat()}T{hhmm}:00",
                  "reason": "proper order"},
        )
        assert r.status_code == 200, r.text
