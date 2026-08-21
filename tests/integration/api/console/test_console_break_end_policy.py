"""API integration — 콘솔 end-break 최소 시간 정책 (HTMA 와 동일 판정).

이 게이트가 없던 동안 같은 행위가 채널에 따라 갈렸다: HTMA 는 29분에서 막히는데
콘솔은 5분짜리 unpaid_meal 도 그대로 닫혀 duration=5 가 저장됐다 (급여 계산은
그 duration 합을 그대로 쓴다).

판정 함수는 키오스크와 공용(`app/utils/break_end_policy.validate_break_end`)이고,
경과는 R2(분 절삭 후 차이)라 화면에 보이는 HH:MM 뺄셈과 일치한다.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from sqlalchemy import select as sa_select

from app.database import async_session
from app.models.attendance_break import AttendanceBreak

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _open_attendance_with_break(
    async_client, admin_headers, make_schedule, test_user, *, break_type: str,
    day_offset: int = -4,
):
    """clock_in 만 찍힌 attendance + 진행 중 break 하나 → (attendance_id, 시작 시각 ISO)."""
    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    target = date.today() + timedelta(days=day_offset)
    sched_id = await make_schedule(
        test_user, work_date=target, start_time=time(9, 0), end_time=time(17, 0)
    )
    async with async_session() as db:
        sched = (
            await db.execute(sa_select(Schedule).where(Schedule.id == sched_id))
        ).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()

    resp = await async_client.get(
        ATT_URL, headers=admin_headers,
        params={"user_id": test_user["id"], "work_date": target.isoformat()},
    )
    att = next(it for it in resp.json()["items"] if it["schedule_id"] == str(sched_id))

    r = await async_client.post(
        f"{ATT_URL}/{att['id']}/actions/clock-in", headers=admin_headers,
        json={"at": f"{target.isoformat()}T09:00:00", "reason": "test setup"},
    )
    assert r.status_code == 200, r.text
    r = await async_client.post(
        f"{ATT_URL}/{att['id']}/actions/start-break", headers=admin_headers,
        json={"at": f"{target.isoformat()}T12:00:00", "break_type": break_type,
              "reason": "test setup"},
    )
    assert r.status_code == 200, r.text
    return att["id"], target


async def _end_break(async_client, admin_headers, att_id, target, hhmmss: str):
    return await async_client.post(
        f"{ATT_URL}/{att_id}/actions/end-break", headers=admin_headers,
        json={"at": f"{target.isoformat()}T{hhmmss}", "reason": "test"},
    )


async def _duration(att_id) -> int | None:
    async with async_session() as db:
        br = (
            await db.execute(
                sa_select(AttendanceBreak).where(
                    AttendanceBreak.attendance_id == att_id
                )
            )
        ).scalars().first()
        return br.duration_minutes if br else None


async def test_console_end_break_rejects_short_meal(
    async_client, admin_headers, make_schedule, test_user
):
    """식사 29분 — 콘솔에서도 400 (예전엔 그대로 닫혀 duration=29 가 저장됐다)."""
    att_id, target = await _open_attendance_with_break(
        async_client, admin_headers, make_schedule, test_user, break_type="unpaid_meal"
    )

    r = await _end_break(async_client, admin_headers, att_id, target, "12:29:00")

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "BREAK_END_TOO_SHORT"
    assert "30-minute minimum" in detail["message"]
    assert await _duration(att_id) is None  # 아무것도 닫히지 않았다


async def test_console_end_break_allows_exactly_30_minutes(
    async_client, admin_headers, make_schedule, test_user
):
    """정확히 30분이면 통과 — 경과는 분 절삭 후 차이(R2)."""
    att_id, target = await _open_attendance_with_break(
        async_client, admin_headers, make_schedule, test_user, break_type="unpaid_meal"
    )

    r = await _end_break(async_client, admin_headers, att_id, target, "12:30:00")

    assert r.status_code == 200, r.text
    assert await _duration(att_id) == 30


async def test_console_end_break_counts_by_displayed_minutes(
    async_client, admin_headers, make_schedule, test_user
):
    """12:00:00 → 12:30:59 은 실제 30분 59초지만 화면상 12:00–12:30 = 30분.

    초를 버리는 게 아니라 **시:분끼리 빼기**라, 타임시트에 보이는 값과 판정이 어긋나지 않는다.
    """
    att_id, target = await _open_attendance_with_break(
        async_client, admin_headers, make_schedule, test_user, break_type="unpaid_meal"
    )

    r = await _end_break(async_client, admin_headers, att_id, target, "12:30:59")

    assert r.status_code == 200, r.text
    assert await _duration(att_id) == 30


async def test_console_end_break_rejects_short_paid_break(
    async_client, admin_headers, make_schedule, test_user
):
    """유급 10분 휴게도 같은 정책 — 9분이면 400."""
    att_id, target = await _open_attendance_with_break(
        async_client, admin_headers, make_schedule, test_user, break_type="paid_10min"
    )

    r = await _end_break(async_client, admin_headers, att_id, target, "12:09:00")

    assert r.status_code == 400, r.text
    assert "10-minute minimum" in r.json()["detail"]["message"]

    # 10분이면 통과
    r = await _end_break(async_client, admin_headers, att_id, target, "12:10:00")
    assert r.status_code == 200, r.text
    assert await _duration(att_id) == 10
