"""Attendance Device — Admin 관리 엔드포인트 테스트.

testadmin (owner) JWT 를 사용해 attendance-devices / access-codes /
clockin-pin 엔드포인트를 호출.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance_device import AttendanceDevice


pytestmark = pytest.mark.asyncio


async def test_admin_list_devices(
    async_client: AsyncClient,
    admin_headers: dict,
    device_auth_headers: dict,
) -> None:
    """device_auth_headers fixture 가 최소 1개의 device 를 만든다."""
    resp = await async_client.get(
        "/api/v1/admin/attendance-devices", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    devices = resp.json()
    assert isinstance(devices, list)
    assert len(devices) >= 1


async def test_admin_rename_device(
    async_client: AsyncClient,
    admin_headers: dict,
    device_auth_headers: dict,
    db: AsyncSession,
) -> None:
    # /me 로 device_id 가져오기
    me = (await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)).json()
    device_id = me["device_id"]

    resp = await async_client.patch(
        f"/api/v1/admin/attendance-devices/{device_id}",
        headers=admin_headers,
        json={"device_name": "Renamed Terminal"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["device_name"] == "Renamed Terminal"


async def test_admin_revoke_device(
    async_client: AsyncClient,
    admin_headers: dict,
    attendance_access_code: str,
    _session_created_device_ids: list,
) -> None:
    # 새 device 생성
    reg = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code},
    )
    body = reg.json()
    token = body["token"]
    device_id = body["device_id"]
    _session_created_device_ids.append(UUID(device_id))

    # admin revoke
    resp = await async_client.delete(
        f"/api/v1/admin/attendance-devices/{device_id}", headers=admin_headers
    )
    assert resp.status_code == 204

    # device 토큰 사용 불가
    me = await async_client.get(
        "/api/v1/attendance/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 401


async def test_admin_get_access_code(
    async_client: AsyncClient,
    admin_headers: dict,
    attendance_access_code: str,
) -> None:
    resp = await async_client.get(
        "/api/v1/admin/access-codes/attendance", headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["service_key"] == "attendance"
    assert body["code"] == attendance_access_code


async def test_admin_rotate_access_code(
    async_client: AsyncClient,
    admin_headers: dict,
    attendance_access_code: str,
) -> None:
    before = attendance_access_code
    try:
        resp = await async_client.post(
            "/api/v1/admin/access-codes/attendance/rotate", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        new_code = resp.json()["code"]
        assert new_code != before

        # 이전 코드로는 등록 실패
        bad = await async_client.post(
            "/api/v1/attendance/register", json={"access_code": before}
        )
        assert bad.status_code == 401

        # 신규 코드로는 성공
        good = await async_client.post(
            "/api/v1/attendance/register", json={"access_code": new_code}
        )
        assert good.status_code == 201
    finally:
        # 원복 — 다른 테스트에 영향 없게
        from app.database import async_session
        from sqlalchemy import text as _text

        async with async_session() as db:
            await db.execute(
                _text("UPDATE access_codes SET code=:c, source='env' WHERE service_key='attendance'"),
                {"c": before},
            )
            await db.commit()


async def test_admin_get_user_clockin_pin(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
) -> None:
    resp = await async_client.get(
        f"/api/v1/admin/users/{test_user['id']}/clockin-pin",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user["id"])
    assert body["clockin_pin"] == test_user["clockin_pin"]


async def test_admin_regenerate_user_clockin_pin(
    async_client: AsyncClient,
    admin_headers: dict,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule,
    restore_pins,
) -> None:
    original_pin = test_user["clockin_pin"]

    # regenerate
    resp = await async_client.post(
        f"/api/v1/admin/users/{test_user['id']}/clockin-pin/regenerate",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    new_pin = resp.json()["clockin_pin"]
    assert new_pin != original_pin
    assert new_pin and len(new_pin) == 6

    # 이전 PIN 으로 clock-in 실패 (400 — device token 은 유효, PIN 불일치)
    r_old = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": original_pin, "user_id": str(test_user["id"])},
    )
    assert r_old.status_code == 400, r_old.text

    # 새 PIN 으로 clock-in 성공
    r_new = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": new_pin, "user_id": str(test_user["id"])},
    )
    assert r_new.status_code == 200, r_new.text


# ── User 생성 시 자동 PIN 발급 ─────────────────────────────────


async def test_admin_create_user_auto_assigns_pin(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    db: AsyncSession,
) -> None:
    """POST /admin/users 직후 GET /admin/users/{id}/clockin-pin 이 6자리 숫자 반환.

    user_service.create_user 내부에서 generate_unique_clockin_pin 을 호출하여
    신규 유저에게 PIN 이 자동 발급되는지 검증.
    """
    import secrets as _secrets
    from sqlalchemy import delete as _delete
    from app.models.user import User

    # testadmin 과 같은 organization 의 staff 역할 조회
    roles_resp = await async_client.get("/api/v1/admin/roles", headers=admin_headers)
    assert roles_resp.status_code == 200, roles_resp.text
    roles = roles_resp.json()
    # priority 가 가장 큰 역할 (가장 하위) 선택 — admin (10) 은 자기 자신보다 하위여야 생성 가능
    staff_role = max(roles, key=lambda r: r["priority"])

    unique_suffix = _secrets.token_hex(4)
    new_username = f"__test_autopin_{unique_suffix}"

    try:
        resp = await async_client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "username": new_username,
                "password": "pw1234",
                "full_name": "Auto PIN Test",
                "role_id": staff_role["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        new_user_id = body["id"]

        # PIN 조회 — 6자리 숫자여야 함
        pin_resp = await async_client.get(
            f"/api/v1/admin/users/{new_user_id}/clockin-pin",
            headers=admin_headers,
        )
        assert pin_resp.status_code == 200, pin_resp.text
        pin_body = pin_resp.json()
        pin = pin_body["clockin_pin"]
        assert pin is not None
        assert isinstance(pin, str)
        assert len(pin) == 6
        assert pin.isdigit()
    finally:
        # 생성된 테스트 유저 제거
        from app.database import async_session as _sess
        async with _sess() as _db:
            await _db.execute(
                _delete(User).where(User.username == new_username)
            )
            await _db.commit()


# NOTE: app self-register 도 동일 로직이지만 email verification token 발급
# 이 필요 — 테스트 과정이 복잡 (SMTP 목/토큰 수동 주입) 하여 현재는 스킵.
# Server 쪽 auth_service.app_register 에서 generate_unique_clockin_pin 호출은
# 소스 레벨로 확인됨.


# ── Net work minutes + display 필드 — admin GET /attendances/{id} ──────


async def _create_completed_attendance(
    db: AsyncSession,
    *,
    organization_id,
    store_id,
    user_id,
    schedule_id,
    work_date_: "date",
    clock_in_dt: "datetime",
    clock_out_dt: "datetime",
    total_work_minutes: int,
    breaks: list[tuple[str, int]],  # [(break_type, duration_minutes), ...]
):
    """DB 에 clocked_out Attendance + AttendanceBreak 행을 직접 삽입.

    build_response 의 집계/차감 로직을 endpoint 경유로 검증하기 위한 헬퍼.
    schedule 은 미리 존재해야 함 (FK).
    """
    from datetime import timedelta as _td
    from app.models.attendance import Attendance
    from app.models.attendance_break import AttendanceBreak

    att = Attendance(
        organization_id=organization_id,
        store_id=store_id,
        user_id=user_id,
        schedule_id=schedule_id,
        work_date=work_date_,
        clock_in=clock_in_dt,
        clock_in_timezone="UTC",
        clock_out=clock_out_dt,
        clock_out_timezone="UTC",
        status="clocked_out",
        total_work_minutes=total_work_minutes,
    )
    db.add(att)
    await db.flush()

    offset = _td(minutes=0)
    for break_type, duration in breaks:
        started = clock_in_dt + offset
        ended = started + _td(minutes=duration)
        br = AttendanceBreak(
            attendance_id=att.id,
            started_at=started,
            ended_at=ended,
            break_type=break_type,
            duration_minutes=duration,
        )
        db.add(br)
        offset += _td(minutes=duration + 1)  # breaks 간 1분 간격
    await db.flush()
    await db.refresh(att)
    return att


async def test_net_work_minutes_deducts_paid_overage(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id,
    make_schedule,
) -> None:
    """paid_short 15분 (10분 초과 → overage 5) + 총근무 120 → net = 115.

    AttendanceResponse.paid_break_overage_minutes == 5, net_work_minutes == 115.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.database import async_session as _sess

    sched_id = await make_schedule(test_user)
    now_utc = _dt.now(_tz.utc).replace(microsecond=0)
    clock_in_dt = now_utc - _td(hours=3)
    clock_out_dt = clock_in_dt + _td(minutes=120)

    async with _sess() as _db:
        att = await _create_completed_attendance(
            _db,
            organization_id=test_user["organization_id"],
            store_id=test_store_id,
            user_id=test_user["id"],
            schedule_id=sched_id,
            work_date_=clock_in_dt.date(),
            clock_in_dt=clock_in_dt,
            clock_out_dt=clock_out_dt,
            total_work_minutes=120,
            breaks=[("paid_short", 15)],
        )
        await _db.commit()
        att_id = str(att.id)

    resp = await async_client.get(
        f"/api/v1/admin/attendances/{att_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_work_minutes"] == 120
    assert body["paid_break_minutes"] == 15
    assert body["unpaid_break_minutes"] == 0
    assert body["paid_break_overage_minutes"] == 5
    assert body["net_work_minutes"] == 115


async def test_net_work_minutes_deducts_unpaid(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id,
    make_schedule,
) -> None:
    """unpaid_long 40 분 → net = total - 40."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.database import async_session as _sess

    sched_id = await make_schedule(test_user)
    now_utc = _dt.now(_tz.utc).replace(microsecond=0)
    clock_in_dt = now_utc - _td(hours=4)
    clock_out_dt = clock_in_dt + _td(minutes=180)

    async with _sess() as _db:
        att = await _create_completed_attendance(
            _db,
            organization_id=test_user["organization_id"],
            store_id=test_store_id,
            user_id=test_user["id"],
            schedule_id=sched_id,
            work_date_=clock_in_dt.date(),
            clock_in_dt=clock_in_dt,
            clock_out_dt=clock_out_dt,
            total_work_minutes=180,
            breaks=[("unpaid_long", 40)],
        )
        await _db.commit()
        att_id = str(att.id)

    resp = await async_client.get(
        f"/api/v1/admin/attendances/{att_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_work_minutes"] == 180
    assert body["unpaid_break_minutes"] == 40
    assert body["paid_break_overage_minutes"] == 0
    assert body["net_work_minutes"] == 140  # 180 - 40 - 0


async def test_net_work_minutes_paid_within_limit_no_deduct(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id,
    make_schedule,
) -> None:
    """paid_short 8분 → overage 0, net == total."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.database import async_session as _sess

    sched_id = await make_schedule(test_user)
    now_utc = _dt.now(_tz.utc).replace(microsecond=0)
    clock_in_dt = now_utc - _td(hours=2)
    clock_out_dt = clock_in_dt + _td(minutes=60)

    async with _sess() as _db:
        att = await _create_completed_attendance(
            _db,
            organization_id=test_user["organization_id"],
            store_id=test_store_id,
            user_id=test_user["id"],
            schedule_id=sched_id,
            work_date_=clock_in_dt.date(),
            clock_in_dt=clock_in_dt,
            clock_out_dt=clock_out_dt,
            total_work_minutes=60,
            breaks=[("paid_short", 8)],
        )
        await _db.commit()
        att_id = str(att.id)

    resp = await async_client.get(
        f"/api/v1/admin/attendances/{att_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_work_minutes"] == 60
    assert body["paid_break_minutes"] == 8
    assert body["paid_break_overage_minutes"] == 0
    assert body["net_work_minutes"] == 60


# ── AttendanceResponse display 필드 검증 ────────────────────────


async def test_attendance_response_includes_display_fields(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id,
    make_schedule,
) -> None:
    """admin GET /attendances/{id} 응답에 display 필드들이 포함되고, 각 break
    item 에도 started_at_display / ended_at_display 가 존재."""
    import re
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.database import async_session as _sess

    sched_id = await make_schedule(test_user)
    now_utc = _dt.now(_tz.utc).replace(microsecond=0)
    clock_in_dt = now_utc - _td(hours=2)
    clock_out_dt = clock_in_dt + _td(minutes=90)

    async with _sess() as _db:
        att = await _create_completed_attendance(
            _db,
            organization_id=test_user["organization_id"],
            store_id=test_store_id,
            user_id=test_user["id"],
            schedule_id=sched_id,
            work_date_=clock_in_dt.date(),
            clock_in_dt=clock_in_dt,
            clock_out_dt=clock_out_dt,
            total_work_minutes=90,
            breaks=[("paid_short", 10)],
        )
        await _db.commit()
        att_id = str(att.id)

    resp = await async_client.get(
        f"/api/v1/admin/attendances/{att_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    hhmm = re.compile(r"^\d{2}:\d{2}$")
    # 최상위 display 필드 존재 + 값 있으면 HH:MM
    for key in [
        "clock_in_display",
        "clock_out_display",
        "scheduled_start_display",
        "scheduled_end_display",
    ]:
        assert key in body, f"missing {key}"
        val = body[key]
        if val is not None:
            assert hhmm.match(val), f"{key}={val} not HH:MM"

    # clock_in/out 은 값이 존재해야 함
    assert body["clock_in_display"] is not None
    assert body["clock_out_display"] is not None

    # breaks[] 의 각 항목도 display 포함
    assert len(body["breaks"]) == 1
    br = body["breaks"][0]
    assert "started_at_display" in br
    assert "ended_at_display" in br
    assert br["started_at_display"] is not None
    assert br["ended_at_display"] is not None
    assert hhmm.match(br["started_at_display"])
    assert hhmm.match(br["ended_at_display"])
