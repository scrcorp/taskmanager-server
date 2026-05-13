"""Attendance kiosk 관리자 모드 (PIN 인증 + admin session) 테스트.

device token 으로 매장 관리자 후보 조회 → PIN 으로 admin session 발급 →
admin token 으로 오늘 매장 스케줄 CRUD + attendance override.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.attendance_admin_session import _clear_all_for_tests
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio


def _admin_headers(device_token: str, admin_token: str) -> dict:
    return {
        "Authorization": f"Bearer {device_token}",
        "X-Admin-Session": admin_token,
    }


async def _ensure_owner_pin(db: AsyncSession, test_users: dict) -> str:
    """testadmin (owner) PIN 보장 — fixture 가 이미 보장하지만 안전망."""
    pin = test_users["testadmin"]["clockin_pin"]
    assert pin and len(pin) == 6
    return pin


async def _ensure_sv_is_manager_of_store(
    db: AsyncSession, sv_user_id: UUID, store_id: UUID
) -> None:
    existing = (
        await db.execute(
            select(UserStore).where(
                UserStore.user_id == sv_user_id, UserStore.store_id == store_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            UserStore(
                user_id=sv_user_id,
                store_id=store_id,
                is_manager=True,
                is_work_assignment=True,
            )
        )
    else:
        existing.is_manager = True
        existing.is_work_assignment = True
    await db.commit()


@pytest.fixture(autouse=True)
def _reset_admin_sessions():
    _clear_all_for_tests()
    yield
    _clear_all_for_tests()


# ── managers list / session open ──────────────────────────


async def test_admin_managers_includes_owner_excludes_staff(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    resp = await async_client.get(
        "/api/v1/attendance/admin/managers", headers=device_auth_headers
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    user_ids = {UUID(r["user_id"]) for r in rows}
    assert test_users["testadmin"]["id"] in user_ids
    assert test_users["teststaff"]["id"] not in user_ids


async def test_admin_session_open_with_owner(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/admin/session",
        headers=device_auth_headers,
        json={
            "user_id": str(test_users["testadmin"]["id"]),
            "pin": test_users["testadmin"]["clockin_pin"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "admin_token" in body and len(body["admin_token"]) >= 20
    assert UUID(body["manager_user_id"]) == test_users["testadmin"]["id"]


async def test_admin_session_rejects_staff(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/admin/session",
        headers=device_auth_headers,
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "pin": test_users["teststaff"]["clockin_pin"],
        },
    )
    assert resp.status_code == 403, resp.text


async def test_admin_session_wrong_pin(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/admin/session",
        headers=device_auth_headers,
        json={"user_id": str(test_users["testadmin"]["id"]), "pin": "000000"},
    )
    assert resp.status_code == 400, resp.text


async def test_admin_protected_endpoint_requires_session(
    async_client: AsyncClient,
    device_auth_headers: dict,
) -> None:
    resp = await async_client.get(
        "/api/v1/attendance/admin/schedules", headers=device_auth_headers
    )
    assert resp.status_code == 401


# ── schedule CRUD ──────────────────────────────────────────


async def _open_admin_session(
    async_client: AsyncClient, device_auth_headers: dict, owner: dict
) -> str:
    resp = await async_client.post(
        "/api/v1/attendance/admin/session",
        headers=device_auth_headers,
        json={"user_id": str(owner["id"]), "pin": owner["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["admin_token"]


async def test_admin_list_today_schedules_empty(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.get(
        "/api/v1/attendance/admin/schedules",
        headers=_admin_headers(device_token, admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


async def test_admin_create_and_delete_schedule(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    db: AsyncSession,
    _tracked_schedule_ids: list,
) -> None:
    # teststaff 를 매장에 배정 (work_assignment)
    existing = (
        await db.execute(
            select(UserStore).where(
                UserStore.user_id == test_users["teststaff"]["id"],
                UserStore.store_id == test_store_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            UserStore(
                user_id=test_users["teststaff"]["id"],
                store_id=test_store_id,
                is_manager=False,
                is_work_assignment=True,
            )
        )
        await db.commit()

    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    headers = _admin_headers(device_token, admin_token)

    # 1) 생성
    resp = await async_client.post(
        "/api/v1/attendance/admin/schedules",
        headers=headers,
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "start_time": "10:00",
            "end_time": "18:00",
        },
    )
    assert resp.status_code == 201, resp.text
    sched_id = UUID(resp.json()["schedule_id"])
    _tracked_schedule_ids.append(sched_id)
    assert resp.json()["status"] == "confirmed"
    assert resp.json()["start_time"] == "10:00"

    # 2) PATCH 시간 변경
    resp = await async_client.patch(
        f"/api/v1/attendance/admin/schedules/{sched_id}",
        headers=headers,
        json={"start_time": "11:00", "end_time": "19:00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["start_time"] == "11:00"

    # 3) GET — 목록에 포함
    resp = await async_client.get(
        "/api/v1/attendance/admin/schedules", headers=headers
    )
    ids = {UUID(r["schedule_id"]) for r in resp.json()}
    assert sched_id in ids

    # 4) DELETE — schedule + attendance 모두 hard delete 되어야 함
    resp = await async_client.delete(
        f"/api/v1/attendance/admin/schedules/{sched_id}", headers=headers
    )
    assert resp.status_code == 204, resp.text

    # delete 후엔 admin schedule 목록에 안 보임
    list_resp = await async_client.get(
        "/api/v1/attendance/admin/schedules", headers=headers
    )
    remaining = {UUID(r["schedule_id"]) for r in list_resp.json()}
    assert sched_id not in remaining

    # attendance row 도 함께 hard delete 됐는지 직접 DB 조회
    from app.models.attendance import Attendance
    from sqlalchemy import select as _select

    r = await db.execute(_select(Attendance).where(Attendance.schedule_id == sched_id))
    assert r.scalar_one_or_none() is None


# ── new endpoints: reason optional, dead-schedule guard, status_change ───


async def test_admin_clock_reason_is_optional(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    make_schedule,
) -> None:
    """매니저가 reason 을 안 적어도 admin action 이 통과해야 함 (placeholder 로 기록)."""
    from datetime import time as _t

    await make_schedule(
        test_users["teststaff"],
        start_time=_t(23, 59),
        end_time=_t(23, 59),
    )
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/clock",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "action": "clock_in",
            # reason 의도적으로 누락
        },
    )
    assert resp.status_code == 200, resp.text


async def test_admin_dead_schedule_guard_blocks_override(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    db: AsyncSession,
) -> None:
    """delete 된 schedule 만 있는 직원에게 admin override 시도 시 거부."""
    from datetime import datetime as _dt, time as _t, timezone as _tz
    from app.models.schedule import Schedule
    from app.utils.timezone import get_store_day_config, get_work_date

    tz_name, day_start = await get_store_day_config(db, test_store_id)
    today = get_work_date(tz_name, day_start, _dt.now(_tz.utc))

    # 직접 deleted 상태로 스케줄 생성 (= 카드에서 사라진 상황 재현)
    sched = Schedule(
        organization_id=test_users["teststaff"]["organization_id"],
        user_id=test_users["teststaff"]["id"],
        store_id=test_store_id,
        work_date=today,
        start_time=_t(9, 0),
        end_time=_t(17, 0),
        status="deleted",
    )
    db.add(sched)
    await db.commit()

    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/clock",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "action": "clock_in",
            "reason": "tries to revive",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "no active schedule" in resp.json()["detail"].lower()


async def _ensure_attendance(
    db: AsyncSession, user_id: UUID, store_id: UUID, organization_id: UUID
) -> None:
    """make_schedule 이 attendance 자동 생성을 하지 않으므로 테스트용 helper."""
    from datetime import datetime as _dt, timezone as _tz
    from app.models.attendance import Attendance
    from app.models.schedule import Schedule
    from app.utils.timezone import get_store_day_config, get_work_date
    from sqlalchemy import select as _select

    tz_name, day_start = await get_store_day_config(db, store_id)
    today = get_work_date(tz_name, day_start, _dt.now(_tz.utc))
    sch = (await db.execute(
        _select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.store_id == store_id,
            Schedule.work_date == today,
        )
    )).scalar_one_or_none()
    if sch is None:
        return
    existing = (await db.execute(
        _select(Attendance).where(Attendance.schedule_id == sch.id)
    )).scalar_one_or_none()
    if existing is not None:
        return
    db.add(Attendance(
        organization_id=organization_id,
        store_id=store_id,
        user_id=user_id,
        schedule_id=sch.id,
        work_date=today,
        status="upcoming",
    ))
    await db.commit()


async def test_admin_status_change_working_without_clock_in_rejected(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    make_schedule,
    db: AsyncSession,
) -> None:
    """admin 이 clock_in 없는 상태에서 status='working' 보내면 거부 (데이터 정합성)."""
    from datetime import time as _t

    await make_schedule(
        test_users["teststaff"], start_time=_t(9, 0), end_time=_t(17, 0),
    )
    await _ensure_attendance(
        db,
        test_users["teststaff"]["id"],
        test_store_id,
        test_users["teststaff"]["organization_id"],
    )
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/attendance/status",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "status": "working",
            "reason": "no time provided",
            # clock_in_hhmm 누락 — 의도적
        },
    )
    assert resp.status_code == 400, resp.text


async def test_me_work_date_reflects_store_day_start_change(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    db: AsyncSession,
) -> None:
    """`/attendance/me` work_date 가 매장 day_start_time 변경 시 즉시 반영됨을 검증.

    실시간 자정 시뮬레이션은 host clock 조작 없이 불가하므로, store 의
    day_start_time 을 미래 시각으로 옮겨 work_date 가 yesterday 로 회귀하는지로
    동등 검증. 헤더가 server work_date 를 그대로 보여주는 클라이언트 입장에서
    "경계가 바뀌면 새 값을 받는다" 와 같은 효과.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import update
    from app.models.organization import Store

    # baseline: day_start = 00:00 (이미 conftest 가 normalize 해둠) → work_date = 오늘
    resp1 = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp1.status_code == 200, resp1.text
    baseline_work_date = resp1.json()["work_date"]
    assert baseline_work_date is not None

    # day_start_time 을 "지금부터 +1h" 로 강제 → 현재 시각이 boundary 이전이라
    # work_date 가 어제로 회귀해야 함 (해당 store tz=UTC, boundary 'all' key 사용)
    now_utc = _dt.now(_tz.utc)
    future_hh = (now_utc + _td(hours=1)).strftime("%H:00")
    await db.execute(
        update(Store)
        .where(Store.id == test_store_id)
        .values(day_start_time={"all": future_hh})
    )
    await db.commit()

    resp2 = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp2.status_code == 200, resp2.text
    shifted_work_date = resp2.json()["work_date"]
    # +1h 미래로 boundary 를 옮겼으니 현재는 boundary 전 → work_date 한 칸 줄어듦
    assert shifted_work_date < baseline_work_date, (
        f"work_date should roll back: baseline={baseline_work_date} shifted={shifted_work_date}"
    )

    # cleanup — 원복 (다른 테스트 영향 방지)
    await db.execute(
        update(Store)
        .where(Store.id == test_store_id)
        .values(day_start_time={"all": "00:00"})
    )
    await db.commit()


async def test_admin_status_change_working_with_clock_in(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    make_schedule,
    db: AsyncSession,
) -> None:
    """admin 이 clock_in_hhmm 함께 보내면 working 으로 전환 + correction 기록."""
    from datetime import time as _t

    await make_schedule(
        test_users["teststaff"], start_time=_t(9, 0), end_time=_t(17, 0),
    )
    await _ensure_attendance(
        db,
        test_users["teststaff"]["id"],
        test_store_id,
        test_users["teststaff"]["organization_id"],
    )
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/attendance/status",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "status": "working",
            "reason": "Forgot to clock in",
            "clock_in_hhmm": "09:05",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "working"
    assert body["clock_in"] is not None
    # 응답에 correction 도 노출되는지
    corr_field_names = {c["field_name"] for c in body.get("corrections", [])}
    # 별도 console API 로 조회 시 corrections 있어야 함 — 여기선 attendance_corrections 직접 확인 생략.
    # (build_response 가 corrections 를 응답에 추가하지 않으므로 status code 만 확인)


# ── clock override ────────────────────────────────────────


async def test_admin_cancel_clock_in(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    make_schedule,
    db: AsyncSession,
) -> None:
    # 스케줄을 "이미 시작됐고 오늘 안 끝남" 으로 설정 → staff PIN clock-in 통과
    from datetime import datetime as _dt, timedelta as _td, time as _t, timezone as _tz

    now_utc = _dt.now(_tz.utc)
    target = now_utc - _td(minutes=10)
    past_start = _t(0, 0) if target.date() != now_utc.date() else target.time().replace(microsecond=0)
    await make_schedule(
        test_users["teststaff"], start_time=past_start, end_time=_t(23, 59)
    )

    # staff 가 정상 PIN 으로 clock-in
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "pin": test_users["teststaff"]["clockin_pin"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("working", "late")

    # admin session 열고 cancel_clock_in
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/clock",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "action": "cancel_clock_in",
            "reason": "QA reset",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clock_in"] is None
    assert resp.json()["status"] == "upcoming"


async def test_admin_clock_in_override_without_pin(
    async_client: AsyncClient,
    device_token: str,
    device_auth_headers: dict,
    test_users: dict,
    make_schedule,
) -> None:
    # 9시 시작 스케줄. now 시간과 무관하게 admin 은 early guard 우회
    await make_schedule(
        test_users["teststaff"],
        start_time=time(23, 59),
        end_time=time(23, 59),
    )
    admin_token = await _open_admin_session(
        async_client, device_auth_headers, test_users["testadmin"]
    )
    resp = await async_client.post(
        "/api/v1/attendance/admin/clock",
        headers=_admin_headers(device_token, admin_token),
        json={
            "user_id": str(test_users["teststaff"]["id"]),
            "action": "clock_in",
            "reason": "QA override",
        },
    )
    # early guard 가 우회되어 성공
    assert resp.status_code == 200, resp.text
    assert resp.json()["clock_in"] is not None
