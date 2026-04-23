"""Attendance Device — clock in/out + break 흐름 테스트.

주의:
    - 매 테스트가 `_cleanup_per_test` 덕분에 빈 상태에서 시작.
    - `test_schedule` 은 teststaff 의 오늘 confirmed schedule 을 자동 생성.
    - break duration 은 초 단위 차이를 분으로 반올림 (int(seconds/60)). 동일
      요청 간 간격이 매우 짧을 수 있으므로 분 값은 `>= 0` 으로만 검증.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.attendance_break import AttendanceBreak


pytestmark = pytest.mark.asyncio


# ── Clock in ──────────────────────────────────────────────────────────


async def test_clock_in_requires_schedule(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
) -> None:
    """스케줄 없이 clock-in 시도 → 400."""
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 400, resp.text
    assert "No scheduled shift" in resp.json()["detail"]


async def test_clock_in_succeeds_with_schedule(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    # test_schedule 은 미래 start_time 으로 설정 (conftest 참조) → 'working'
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "working"
    assert body["schedule_id"] == str(test_schedule)
    assert body["clock_in"] is not None


async def test_clock_in_late_marks_status_and_anomaly(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    make_schedule,
) -> None:
    """scheduled_start 가 과거 → status='late', anomalies에 'late' 포함.

    LATE_BUFFER_MINUTES=0 이므로 1분만 늦어도 late. UTC 00:00~00:29 구간에서는
    오늘 00:00 부터 과거로 돌아갈 수 없으므로 자정 이전 실행 시에만 의미있다;
    그 경우 work_date 0시를 start 로 써 여전히 now > scheduled_start 가 되게 한다.
    """
    from datetime import datetime as _dt, time as _time, timedelta as _td, timezone as _tz

    now_utc = _dt.now(_tz.utc)
    target = now_utc - _td(minutes=30)
    if target.date() != now_utc.date():
        # 자정 직후 → 오늘 00:00 을 start 로 (30분 전보다는 덜 하지만 now > start)
        past_start = _time(0, 0)
    else:
        past_start = target.time().replace(microsecond=0)
    past_end = _time(23, 59)
    await make_schedule(test_user, start_time=past_start, end_time=past_end)

    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "late"
    assert body["anomalies"] is not None
    assert "late" in body["anomalies"]


async def test_clock_in_twice_today(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    """이미 working 중인 상태에서 재 clock_in → 'Previous shift not clocked out' 400.

    clocked_out 인 상태에서 재출근은 'Already clocked in today' 로 거절 (별도 테스트).
    """
    r1 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r1.status_code == 200, r1.text

    r2 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r2.status_code == 400, r2.text
    detail = r2.json()["detail"]
    # working 중이면 "Previous shift not clocked out" (우선 체크).
    assert (
        "Previous shift not clocked out" in detail
        or "Already clocked in today" in detail
    ), detail


async def test_clock_in_after_clock_out_blocked(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    """clocked_out 상태에서 다시 clock_in → 'Already clocked in today' 400."""
    # 출근
    r1 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r1.status_code == 200, r1.text
    # 퇴근
    r2 = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r2.status_code == 200, r2.text
    # 재 출근 금지
    r3 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r3.status_code == 400, r3.text
    assert "Already clocked in today" in r3.json()["detail"]


async def test_clock_out_before_clock_in(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 400
    assert "Must clock in first" in resp.json()["detail"]


# ── Break flows ────────────────────────────────────────────────────────


async def test_short_paid_break_flow(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    test_schedule: UUID,
    db: AsyncSession,
) -> None:
    # clock in
    r1 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r1.status_code == 200

    # break start (paid_short)
    r2 = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"]), "break_type": "paid_short"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "on_break"

    # check DB row — test_store_id 로 필터 (다른 매장의 과거 attendance 무시)
    att = (
        await db.execute(
            select(Attendance).where(
                Attendance.user_id == test_user["id"],
                Attendance.store_id == test_store_id,
            )
        )
    ).scalar_one()
    breaks = (
        await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == att.id)
        )
    ).scalars().all()
    assert len(breaks) == 1
    assert breaks[0].break_type == "paid_short"
    assert breaks[0].ended_at is None

    # break end
    r3 = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "working"

    await db.commit()  # release any snapshot
    await db.close()
    # 재조회
    from app.database import async_session

    async with async_session() as fresh:
        br = (
            await fresh.execute(select(AttendanceBreak).where(AttendanceBreak.id == breaks[0].id))
        ).scalar_one()
        assert br.ended_at is not None
        assert (br.duration_minutes or 0) >= 0


async def test_long_unpaid_break_flow(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
    db: AsyncSession,
) -> None:
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    r2 = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"]), "break_type": "unpaid_long"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "on_break"

    r3 = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "working"
    # unpaid 는 net_work_minutes 에서 빠짐 (응답 필드 존재 확인)
    assert "unpaid_break_minutes" in r3.json()


async def test_break_start_without_type_fails(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    resp = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},  # no break_type
    )
    assert resp.status_code == 400
    assert "break_type" in resp.json()["detail"]


async def test_double_break_start_blocked(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    r1 = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"]), "break_type": "paid_short"},
    )
    assert r1.status_code == 200

    r2 = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"]), "break_type": "paid_short"},
    )
    assert r2.status_code == 400
    # 현재 status=on_break 이므로 "Cannot start break in current state"
    # 또는 구현에 따라 "A break is already in progress" — 둘 중 하나 메시지 검증
    detail = r2.json()["detail"]
    assert "break" in detail.lower()


async def test_break_end_without_open_break(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    resp = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 400
    assert "Not currently on break" in resp.json()["detail"] or "No open break" in resp.json()["detail"]


async def test_clock_out_closes_open_break(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    test_schedule: UUID,
    db: AsyncSession,
) -> None:
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"]), "break_type": "paid_short"},
    )
    # clock out 직접 호출 — 진행 중 break 자동 종료 기대
    r_out = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r_out.status_code == 200, r_out.text
    assert r_out.json()["status"] == "clocked_out"

    from app.database import async_session

    async with async_session() as fresh:
        att = (
            await fresh.execute(
                select(Attendance).where(
                    Attendance.user_id == test_user["id"],
                    Attendance.store_id == test_store_id,
                )
            )
        ).scalar_one()
        breaks = (
            await fresh.execute(
                select(AttendanceBreak).where(AttendanceBreak.attendance_id == att.id)
            )
        ).scalars().all()
        assert len(breaks) == 1
        assert breaks[0].ended_at is not None


# ── PIN ───────────────────────────────────────────────────────────────


async def test_pin_wrong(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    """올바른 user_id + 틀린 PIN → 400 (device token 은 유효)."""
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": "000000", "user_id": str(test_user["id"])},
    )
    # PIN 오류는 400. 401 은 device token 실패에만 사용.
    assert resp.status_code == 400, resp.text
    assert "Invalid PIN" in resp.json()["detail"]


async def test_pin_format_invalid(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
) -> None:
    # 6자 미만 → Pydantic 422 또는 서비스 단 400
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": "123", "user_id": str(test_user["id"])},
    )
    assert resp.status_code in (400, 422), resp.text


async def test_clock_in_with_wrong_pin_for_user(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    """올바른 user_id + 틀린 PIN → 400, device token 유지."""
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": "999999", "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 400, resp.text
    assert "Invalid PIN" in resp.json()["detail"]

    # 이어서 정상 PIN 요청 → device token 이 살아있어 성공해야 함
    resp_ok = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp_ok.status_code == 200, resp_ok.text


async def test_clock_in_with_mismatched_user_pin(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    make_schedule,
) -> None:
    """다른 유저의 PIN 값으로 요청 → 400. user→pin 검증 방식이므로 실패해야.

    기존 pin→user 매핑이라면 other_user 의 PIN 을 보내면 그 유저로 해석되어
    통과했겠지만, 현재는 요청한 user_id 의 PIN 과 다르므로 400.
    """
    teststaff = test_users["teststaff"]
    testsv = test_users["testsv"]
    # teststaff 로 스케줄 만들고, teststaff 의 user_id 로 요청하되 testsv 의 PIN 을 전달
    await make_schedule(teststaff)
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": testsv["clockin_pin"], "user_id": str(teststaff["id"])},
    )
    assert resp.status_code == 400, resp.text
    assert "Invalid PIN" in resp.json()["detail"]


# ── no_show → clock_in 업데이트 ────────────────────────────────────────


async def test_clock_in_on_no_show_updates_existing_row(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
    db: AsyncSession,
) -> None:
    """cron 이 만든 no_show row 에 대해 clock_in → 새 row 안 만들고 기존 업데이트."""
    from datetime import datetime as _dt, time as _time, timedelta as _td, timezone as _tz
    from app.database import async_session
    from app.models.attendance import Attendance

    # 오늘 과거 스케줄 생성 (clock_in 시점에 'late' 로 업데이트될 것)
    now_utc = _dt.now(_tz.utc)
    target = now_utc - _td(minutes=30)
    past_start = _time(0, 0) if target.date() != now_utc.date() else target.time().replace(microsecond=0)
    sched_id = await make_schedule(
        test_user, start_time=past_start, end_time=_time(23, 59)
    )

    # 수동으로 no_show attendance row 삽입 (cron 이 만든 상황 재현)
    async with async_session() as sess:
        att = Attendance(
            organization_id=test_user["organization_id"],
            store_id=test_store_id,
            user_id=test_user["id"],
            schedule_id=sched_id,
            work_date=now_utc.date(),
            status="no_show",
            anomalies=["no_show"],
        )
        sess.add(att)
        await sess.commit()
        existing_id = att.id

    # clock_in 호출
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # status 는 working 또는 late (과거 start 이므로 late)
    assert body["status"] in ("working", "late")
    assert body["clock_in"] is not None
    # 같은 id 여야 함 — 새 row 가 생긴 것이 아님
    assert body["id"] == str(existing_id)

    # DB 확인
    async with async_session() as sess:
        rows = (
            await sess.execute(
                select(Attendance).where(
                    Attendance.user_id == test_user["id"],
                    Attendance.work_date == now_utc.date(),
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == existing_id
        assert row.clock_in is not None
        assert row.status in ("working", "late")
        anomalies = list(row.anomalies or [])
        assert "no_show" not in anomalies


# ── 2 schedule (연속 shift) 케이스 ─────────────────────────────────────


async def test_clock_in_blocked_when_previous_shift_not_clocked_out(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    make_schedule,
) -> None:
    """첫 schedule 에 working 중인데 두 번째 schedule 로 clock_in → 400."""
    from datetime import time as _time

    await make_schedule(test_user, start_time=_time(9, 0), end_time=_time(13, 0))
    await make_schedule(test_user, start_time=_time(14, 0), end_time=_time(18, 0))

    # 첫 출근 → working
    r1 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r1.status_code == 200, r1.text
    # status 는 working 또는 late
    assert r1.json()["status"] in ("working", "late")

    # 두 번째 clock_in → 차단
    r2 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert r2.status_code == 400, r2.text
    assert "Previous shift not clocked out" in r2.json()["detail"]


async def test_clock_in_multiple_schedules_picks_current_window(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    make_schedule,
) -> None:
    """2 schedules (오전 과거, 현재 창) → 현재 window 에 속한 스케줄 선택."""
    from datetime import datetime as _dt, time as _time, timedelta as _td, timezone as _tz

    now_utc = _dt.now(_tz.utc)
    # 오전 (이미 끝남): 00:00 ~ 아주 오래전
    morning_end = (now_utc - _td(hours=1)).time().replace(microsecond=0)
    morning_start = _time(0, 0)
    # 현재 창: 방금 전 ~ 먼 미래
    current_start = (now_utc - _td(minutes=10)).time().replace(microsecond=0)
    current_end = _time(23, 59)

    morning_id = await make_schedule(
        test_user, start_time=morning_start, end_time=morning_end
    )
    current_id = await make_schedule(
        test_user, start_time=current_start, end_time=current_end
    )

    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"], "user_id": str(test_user["id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 선택된 schedule 은 현재 창 (current_id)
    assert body["schedule_id"] == str(current_id)
    # (참고: morning_id 도 존재하지만 선택되면 안 됨)
    assert body["schedule_id"] != str(morning_id)
