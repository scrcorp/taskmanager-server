"""API integration — 감사 채널(attendance_corrections.channel) 기록 검증.

channel 은 "어느 클라이언트 표면 경로로 이 정정이 들어왔나"를 남기는 감사
메타데이터다 (app/core/client_surface.py + ClientSurfaceMiddleware).

- 콘솔 API 경로(/api/v1/console/*) 기본값 = "console"
- `X-Client-Surface` 헤더는 허용 목록(console / console_compact) 내 값만
  기본값을 덮어쓴다 — /c 간소화 콘솔이 같은 API 를 쓰기 때문에 필요한 세분화
- 허용 목록 밖 값은 무시하고 경로 기본값 유지 (헤더는 보안 경계가 아니다)

주의: attendance_corrections.channel 마이그레이션 반영 후 실행할 것.
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta

import pytest

from app.database import async_session

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _seeded_attendance(async_client, admin_headers, make_schedule, test_user):
    """과거 스케줄 + attendance 행 생성 후 attendance_id 반환."""
    from sqlalchemy import select as sa_select

    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    target = date.today() - timedelta(days=4)
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
        ATT_URL,
        headers=admin_headers,
        params={"user_id": test_user["id"], "work_date": target.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    att = next(it for it in resp.json()["items"] if it["schedule_id"] == str(sched_id))
    return att["id"], target


async def _correct_clock_in(async_client, headers, att_id, work_date, hhmm: str):
    """clock_in 정정 호출 후 생성된 correction id 반환."""
    r = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        headers=headers,
        json={
            "field_name": "clock_in",
            "corrected_value": f"{work_date.isoformat()}T{hhmm}",
            "reason": "channel audit test",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _channel_of(correction_id: str) -> str | None:
    """DB 에서 correction 행의 channel 을 직접 읽는다.

    응답 스키마(AttendanceCorrectionResponse)는 아직 channel 을 노출하지
    않으므로 감사 행 자체를 검증한다.
    """
    from sqlalchemy import select as sa_select

    from app.models.attendance import AttendanceCorrection

    async with async_session() as db:
        row = (
            await db.execute(
                sa_select(AttendanceCorrection).where(
                    AttendanceCorrection.id == uuid.UUID(correction_id)
                )
            )
        ).scalar_one()
        return row.channel


async def test_console_correct_records_console_channel(
    async_client, admin_headers, make_schedule, test_user
):
    """콘솔 경로(헤더 없음) 정정 → channel == "console"."""
    att_id, work_date = await _seeded_attendance(
        async_client, admin_headers, make_schedule, test_user
    )
    corr_id = await _correct_clock_in(
        async_client, admin_headers, att_id, work_date, "09:05"
    )
    assert await _channel_of(corr_id) == "console"


async def test_compact_header_records_console_compact_channel(
    async_client, admin_headers, make_schedule, test_user
):
    """`X-Client-Surface: console_compact` 헤더 → channel == "console_compact"."""
    att_id, work_date = await _seeded_attendance(
        async_client, admin_headers, make_schedule, test_user
    )
    headers = {**admin_headers, "X-Client-Surface": "console_compact"}
    corr_id = await _correct_clock_in(
        async_client, headers, att_id, work_date, "09:10"
    )
    assert await _channel_of(corr_id) == "console_compact"


async def test_disallowed_header_falls_back_to_path_default(
    async_client, admin_headers, make_schedule, test_user
):
    """허용 목록 밖 헤더 값("hacker")은 무시 → 경로 기본값 "console" 유지."""
    att_id, work_date = await _seeded_attendance(
        async_client, admin_headers, make_schedule, test_user
    )
    headers = {**admin_headers, "X-Client-Surface": "hacker"}
    corr_id = await _correct_clock_in(
        async_client, headers, att_id, work_date, "09:15"
    )
    assert await _channel_of(corr_id) == "console"
