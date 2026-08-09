"""API integration — 초가 섞인 기록의 분 단위 절삭 규칙(R2) 회귀 테스트.

규칙: 표시는 `HH:MM` (초 버림) 이고 계산은 "각 시각을 분으로 절삭한 뒤 차이".
따라서 화면의 시각끼리 뺀 값과 API 가 주는 분 값이 항상 일치해야 한다.

관측된 버그: break 22:26:50 → 22:57:20 이 화면엔 `22:26 – 22:57` (31분) 로
보이는데 duration_minutes 는 30 이었다 (차이를 내림했기 때문).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.database import async_session
from app.utils.timezone import get_store_timezone

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _seeded_attendance(async_client, admin_headers, make_schedule, test_user):
    """과거 스케줄 + attendance 행 생성 후 (attendance_id, work_date) 반환."""
    from sqlalchemy import select as sa_select

    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    target = date.today() - timedelta(days=4)
    sched_id = await make_schedule(
        test_user, work_date=target, start_time=time(17, 30), end_time=time(23, 30)
    )
    async with async_session() as db:
        sched = (
            await db.execute(sa_select(Schedule).where(Schedule.id == sched_id))
        ).scalar_one()
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()

    resp = await async_client.get(
        ATT_URL,
        headers=admin_headers,
        params={"user_id": test_user["id"], "work_date": target.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    att = next(it for it in resp.json()["items"] if it["schedule_id"] == str(sched_id))
    return att["id"], target


async def _correct(async_client, admin_headers, att_id, field, value):
    r = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        headers=admin_headers,
        json={"field_name": field, "corrected_value": value, "reason": "truncation test"},
    )
    assert r.status_code == 200, r.text
    return r


async def test_seconds_are_truncated_before_minute_math(
    async_client, admin_headers, make_schedule, test_user, test_store_id
):
    """clock_in 18:01:40 / clock_out 23:04:10 / break 22:26:50~22:57:20.

    표시는 18:01, 23:04, 22:26–22:57 이므로
      total = 23:04 − 18:01 = 303분
      break = 22:57 − 22:26 = 31분   (차이를 내림하면 30 — 그러면 안 됨)
      net   = 303 − 31 = 272분
    """
    att_id, target = await _seeded_attendance(
        async_client, admin_headers, make_schedule, test_user
    )
    d = target.isoformat()

    await _correct(async_client, admin_headers, att_id, "clock_in", f"{d}T18:01:40")
    await _correct(async_client, admin_headers, att_id, "clock_out", f"{d}T23:04:10")

    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{d}T22:26:50",
            "ended_at": f"{d}T22:57:20",
            "break_type": "unpaid_meal",
        },
    )
    assert r.status_code in (200, 201), r.text

    detail = (await async_client.get(f"{ATT_URL}/{att_id}", headers=admin_headers)).json()

    async with async_session() as db:
        tz = ZoneInfo(await get_store_timezone(db, test_store_id))

    def hhmm(iso: str) -> str:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")

    # 저장은 초까지 보존 (R1)
    stored_in = datetime.fromisoformat(detail["clock_in"].replace("Z", "+00:00"))
    assert stored_in.astimezone(tz).second == 40

    # 표시 시각
    assert hhmm(detail["clock_in"]) == "18:01"
    assert hhmm(detail["clock_out"]) == "23:04"
    brk = detail["breaks"][0]
    assert hhmm(brk["started_at"]) == "22:26"
    assert hhmm(brk["ended_at"]) == "22:57"

    # 지표 — 표시 시각끼리의 뺄셈과 일치해야 한다
    assert brk["duration_minutes"] == 31
    assert detail["total_work_minutes"] == 303
    assert detail["net_work_minutes"] == 272


async def test_within_same_minute_is_zero(
    async_client, admin_headers, make_schedule, test_user
):
    """같은 분 안에서 시작·종료하면 표시가 같은 HH:MM 이므로 0분."""
    att_id, target = await _seeded_attendance(
        async_client, admin_headers, make_schedule, test_user
    )
    d = target.isoformat()
    await _correct(async_client, admin_headers, att_id, "clock_in", f"{d}T18:00:00")
    await _correct(async_client, admin_headers, att_id, "clock_out", f"{d}T23:00:00")

    r = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        headers=admin_headers,
        json={
            "started_at": f"{d}T20:10:05",
            "ended_at": f"{d}T20:10:55",
            "break_type": "paid_10min",
        },
    )
    assert r.status_code in (200, 201), r.text

    detail = (await async_client.get(f"{ATT_URL}/{att_id}", headers=admin_headers)).json()
    assert detail["breaks"][0]["duration_minutes"] == 0
    # paid 10분 이내라 net 차감 없음
    assert detail["net_work_minutes"] == detail["total_work_minutes"] == 300
