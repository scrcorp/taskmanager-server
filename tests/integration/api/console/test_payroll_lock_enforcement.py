"""API integration — L3 lock enforcement (확정 pay period 안 날짜의 mutation 차단).

confirmed pay period 안의 날짜를 대상으로 하는 모든 콘솔 mutation 이
409 + "Pay period for this date is confirmed and locked" 로 거부되는지,
기간 밖(경계 다음 날)은 정상 통과하는지 검증한다.

경로별 커버:
    - PATCH /console/attendances/{id}/correct
    - 7개 state-machine 액션 (/attendances/{id}/actions/*)
    - break 세션 CRUD (/attendances/{id}/breaks*)
    - POST /console/schedules (소급 생성 차단) — locked 과거 날짜만, 기간 밖 과거는 허용
    - PATCH/DELETE /console/schedules/{id} + cancel/revert/confirm
    - PATCH 로 미잠금 스케줄을 locked 기간으로 이동시키는 우회 차단
    - DELETE /console/schedules/bulk (bulk 경로 — 실패 집계 + 에러 메시지)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.attendance_break import AttendanceBreak
from app.models.payroll import PayPeriod
from app.models.schedule import Schedule
from app.models.user_store import UserStore
from app.services.payroll_lock_service import PAY_PERIOD_LOCKED_MESSAGE
from app.services.payroll_period_service import prev_period_bounds

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"
SCHED_URL = "/api/v1/console/schedules"


# ---------------------------------------------------------------------------
# 픽스처 — 직전 반월(항상 완전히 과거)을 confirmed 로 잠근다
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def locked_env(
    seed_organization: dict, test_users: dict, test_store_id: UUID,
) -> AsyncIterator[dict]:
    """test store 의 직전 pay period 를 confirmed 로 삽입 + staff 배정.

    confirm 서비스는 병렬 트랙 소유라 상태를 픽스처에서 직접 만든다 (스펙 §4
    라이프사이클과 동일한 행). attendance/schedule row 정리는 conftest 의
    _clean_state 가 담당, pay_periods 만 여기서 정리.
    """
    org_id: UUID = seed_organization["id"]
    staff = test_users["teststaff"]
    today = datetime.now(timezone.utc).date()
    start, end = prev_period_bounds(today)  # 직전 반월 — 항상 오늘 이전에 종료

    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == test_store_id))
        db.add(PayPeriod(
            organization_id=org_id,
            store_id=test_store_id,
            start_date=start,
            end_date=end,
            status="confirmed",
        ))
        # staff 를 work-assignment 로 배정 (스케줄 검증 통과용)
        existing = (await db.execute(
            select(UserStore).where(
                UserStore.user_id == staff["id"], UserStore.store_id == test_store_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(
                user_id=staff["id"], store_id=test_store_id, is_work_assignment=True,
            ))
        else:
            existing.is_work_assignment = True
        await db.commit()

    env = {
        "org_id": org_id,
        "store_id": test_store_id,
        "staff": staff,
        "locked_date": start,          # 기간 안 (중간이 아닌 시작일이지만 동일 판정)
        "locked_end": end,             # 경계 — end_date 도 잠김
        "after_end": end + timedelta(days=1),  # 기간 밖 과거/오늘 — 통과해야 함
    }
    yield env

    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == test_store_id))
        await db.execute(delete(UserStore).where(
            UserStore.user_id == staff["id"], UserStore.store_id == test_store_id,
        ))
        await db.commit()


async def _make_attendance(
    env: dict, work_date: date, *, status: str = "clocked_out",
    with_times: bool = True,
) -> UUID:
    """워크인(schedule 없는) attendance row 직접 생성 — 과거 날짜 게이트 테스트용."""
    clock_in = datetime.combine(work_date, time(9, 0), tzinfo=timezone.utc)
    clock_out = datetime.combine(work_date, time(17, 0), tzinfo=timezone.utc)
    async with async_session() as db:
        att = Attendance(
            organization_id=env["org_id"],
            store_id=env["store_id"],
            user_id=env["staff"]["id"],
            schedule_id=None,
            work_date=work_date,
            clock_in=clock_in if with_times else None,
            clock_in_timezone="UTC" if with_times else None,
            clock_out=clock_out if with_times else None,
            clock_out_timezone="UTC" if with_times else None,
            status=status if with_times else "upcoming",
            total_work_minutes=480 if with_times else None,
        )
        db.add(att)
        await db.commit()
        return att.id


async def _make_schedule(env: dict, operating_day: date, *, status: str = "confirmed") -> UUID:
    async with async_session() as db:
        sched = Schedule(
            organization_id=env["org_id"],
            user_id=env["staff"]["id"],
            store_id=env["store_id"],
            operating_day=operating_day,
            start_at=datetime.combine(operating_day, time(9, 0)),
            end_at=datetime.combine(operating_day, time(17, 0)),
            status=status,
        )
        db.add(sched)
        await db.commit()
        return sched.id


def _assert_locked_409(resp) -> None:
    assert resp.status_code == 409, resp.text
    assert PAY_PERIOD_LOCKED_MESSAGE in str(resp.json()["detail"])


# ---------------------------------------------------------------------------
# Attendance — correction
# ---------------------------------------------------------------------------


async def test_correction_locked_date_409(async_client, admin_headers, locked_env):
    att_id = await _make_attendance(locked_env, locked_env["locked_date"])
    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        json={
            "field_name": "clock_out",
            "corrected_value": f"{locked_env['locked_date'].isoformat()}T17:30",
            "reason": "late fix attempt",
        },
        headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_correction_boundary_end_date_409(async_client, admin_headers, locked_env):
    """경계: 기간 end_date 도 잠김."""
    att_id = await _make_attendance(locked_env, locked_env["locked_end"])
    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        json={
            "field_name": "clock_out",
            "corrected_value": f"{locked_env['locked_end'].isoformat()}T17:30",
            "reason": "boundary fix",
        },
        headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_correction_day_after_period_end_passes(async_client, admin_headers, locked_env):
    """경계: end_date 다음 날은 잠기지 않음 — 정정 성공."""
    att_id = await _make_attendance(locked_env, locked_env["after_end"])
    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        json={
            "field_name": "clock_out",
            "corrected_value": f"{locked_env['after_end'].isoformat()}T17:30",
            "reason": "valid fix",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Attendance — 7개 state-machine 액션 전부 잠김
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,payload",
    [
        ("clock-in", {"at": "{d}T09:00", "reason": "x"}),
        ("clock-out", {"at": "{d}T17:00", "reason": "x"}),
        ("start-break", {"at": "{d}T12:00", "break_type": "paid_10min", "reason": "x"}),
        ("end-break", {"at": "{d}T12:10", "reason": "x"}),
        ("mark-no-show", {"reason": "x"}),
        ("cancel", {"reason": "x"}),
        ("reopen", {"reason": "x"}),
    ],
)
async def test_action_endpoints_locked_409(
    async_client, admin_headers, locked_env, action, payload,
):
    att_id = await _make_attendance(locked_env, locked_env["locked_date"])
    d = locked_env["locked_date"].isoformat()
    body = {k: (v.format(d=d) if isinstance(v, str) else v) for k, v in payload.items()}
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/actions/{action}", json=body, headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_action_unlocked_date_passes(async_client, admin_headers, locked_env):
    """기간 밖 날짜의 액션은 lock 게이트를 통과한다 (reopen: clocked_out → working)."""
    att_id = await _make_attendance(locked_env, locked_env["after_end"])
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/actions/reopen", json={"reason": "undo"}, headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "working"


# ---------------------------------------------------------------------------
# Attendance — break 세션 CRUD
# ---------------------------------------------------------------------------


async def test_break_add_locked_409(async_client, admin_headers, locked_env):
    att_id = await _make_attendance(locked_env, locked_env["locked_date"])
    d = locked_env["locked_date"].isoformat()
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        json={"started_at": f"{d}T12:00:00Z", "ended_at": f"{d}T12:10:00Z",
              "break_type": "paid_10min"},
        headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_break_update_and_delete_locked_409(async_client, admin_headers, locked_env):
    att_id = await _make_attendance(locked_env, locked_env["locked_date"])
    d = locked_env["locked_date"]
    async with async_session() as db:
        br = AttendanceBreak(
            attendance_id=att_id,
            started_at=datetime.combine(d, time(12, 0), tzinfo=timezone.utc),
            ended_at=datetime.combine(d, time(12, 10), tzinfo=timezone.utc),
            break_type="paid_10min",
            duration_minutes=10,
        )
        db.add(br)
        await db.commit()
        br_id = br.id

    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/breaks/{br_id}",
        json={"break_type": "unpaid_meal"},
        headers=admin_headers,
    )
    _assert_locked_409(resp)

    resp = await async_client.delete(
        f"{ATT_URL}/{att_id}/breaks/{br_id}", headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_break_add_unlocked_passes(async_client, admin_headers, locked_env):
    att_id = await _make_attendance(locked_env, locked_env["after_end"])
    d = locked_env["after_end"].isoformat()
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/breaks",
        json={"started_at": f"{d}T12:00:00Z", "ended_at": f"{d}T12:10:00Z",
              "break_type": "paid_10min"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Schedule — 소급 생성/수정/삭제/취소/되돌리기/확정 차단
# ---------------------------------------------------------------------------


async def test_schedule_create_past_locked_409(async_client, admin_headers, locked_env):
    """소급 차단 — locked 기간의 과거 날짜로 스케줄 생성 금지."""
    resp = await async_client.post(SCHED_URL, json={
        "user_id": str(locked_env["staff"]["id"]),
        "store_id": str(locked_env["store_id"]),
        "work_date": locked_env["locked_date"].isoformat(),
        "start_time": "09:00", "end_time": "17:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    _assert_locked_409(resp)


async def test_schedule_create_boundary_end_409_after_end_passes(
    async_client, admin_headers, locked_env,
):
    """경계: end_date 생성 409, end_date+1 생성 성공 (기간 밖 과거는 열려 있음)."""
    resp = await async_client.post(SCHED_URL, json={
        "user_id": str(locked_env["staff"]["id"]),
        "store_id": str(locked_env["store_id"]),
        "work_date": locked_env["locked_end"].isoformat(),
        "start_time": "09:00", "end_time": "17:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    _assert_locked_409(resp)

    resp = await async_client.post(SCHED_URL, json={
        "user_id": str(locked_env["staff"]["id"]),
        "store_id": str(locked_env["store_id"]),
        "work_date": locked_env["after_end"].isoformat(),
        "start_time": "09:00", "end_time": "17:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text


async def test_schedule_update_locked_409(async_client, admin_headers, locked_env):
    sched_id = await _make_schedule(locked_env, locked_env["locked_date"])
    resp = await async_client.patch(
        f"{SCHED_URL}/{sched_id}", json={"start_time": "10:00"}, headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_schedule_move_into_locked_period_409(async_client, admin_headers, locked_env):
    """우회 차단 — 미잠금 스케줄의 날짜를 locked 기간으로 옮기기 금지."""
    sched_id = await _make_schedule(locked_env, locked_env["after_end"])
    resp = await async_client.patch(
        f"{SCHED_URL}/{sched_id}",
        json={"work_date": locked_env["locked_date"].isoformat(),
              "start_time": "09:00", "end_time": "17:00", "force": True},
        headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_schedule_delete_locked_409(async_client, admin_headers, locked_env):
    sched_id = await _make_schedule(locked_env, locked_env["locked_date"])
    resp = await async_client.delete(f"{SCHED_URL}/{sched_id}", headers=admin_headers)
    _assert_locked_409(resp)


async def test_schedule_cancel_locked_409(async_client, admin_headers, locked_env):
    sched_id = await _make_schedule(locked_env, locked_env["locked_date"])
    resp = await async_client.post(
        f"{SCHED_URL}/{sched_id}/cancel",
        json={"cancellation_reason": "x"}, headers=admin_headers,
    )
    _assert_locked_409(resp)


async def test_schedule_revert_locked_409(async_client, admin_headers, locked_env):
    sched_id = await _make_schedule(locked_env, locked_env["locked_date"])
    resp = await async_client.post(f"{SCHED_URL}/{sched_id}/revert", headers=admin_headers)
    _assert_locked_409(resp)


async def test_schedule_confirm_locked_409(async_client, admin_headers, locked_env):
    """requested → confirmed 소급 확정도 차단."""
    sched_id = await _make_schedule(
        locked_env, locked_env["locked_date"], status="requested",
    )
    resp = await async_client.post(f"{SCHED_URL}/{sched_id}/confirm", headers=admin_headers)
    _assert_locked_409(resp)


async def test_schedule_bulk_delete_locked_collected_as_failed(
    async_client, admin_headers, locked_env,
):
    """bulk 경로 — locked 건은 failed 로 집계되고 에러 메시지가 남는다."""
    locked_id = await _make_schedule(locked_env, locked_env["locked_date"])
    open_id = await _make_schedule(locked_env, locked_env["after_end"])
    resp = await async_client.request(
        "DELETE", f"{SCHED_URL}/bulk",
        json={"ids": [str(locked_id), str(open_id)]},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["failed"] == 1
    assert any(PAY_PERIOD_LOCKED_MESSAGE in e for e in body["errors"])


# ---------------------------------------------------------------------------
# 신청(request) 플로우 테스트는 기능 폐기(2026-08-09)와 함께 제거.
# 급여 잠금 자체는 위의 스케줄 create/update/delete/bulk 경로에서 계속 검증된다.
# ---------------------------------------------------------------------------
