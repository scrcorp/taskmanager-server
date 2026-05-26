"""API integration tests — POST /api/v1/attendance/identify-by-pin.

[작성됨] — 이번 phase
- 정상 식별 (스케줄 있음 → today_status, 스케줄 없음 → None)
- PIN 매치 없음 → 400 'Invalid PIN'
- 비활성 user 의 PIN → 400 (식별 안 됨)
- soft-deleted user 의 PIN → 400 (식별 안 됨)
- PIN 형식 위반 (5자리 / 7자리 / 비숫자) → 422 (Pydantic)
- device token 없음 → 403
- device 가 store 미할당 → today_status=None
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.asyncio


# ── happy path ───────────────────────────────────────────────


async def test_identify_by_pin_returns_user_info_no_schedule(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
) -> None:
    """오늘 스케줄 없는 user → user 정보 + today_status=None."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user["id"])
    assert body["user_name"] == test_user["full_name"]
    assert body["today_status"] is None
    # Stage J 신규 응답 필드
    assert body["current_break"] is None
    assert body["scheduled_end"] is None


async def test_identify_by_pin_returns_today_status_when_schedule_exists(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule,  # fixture 가 오늘 confirmed schedule + attendance 생성
) -> None:
    """오늘 스케줄(upcoming) 있는 user → today_status != None."""
    # test_schedule 가 만든 schedule + attendance 가 있어야 함
    # attendance 가 자동 생성되는 건 schedule 만들 때가 아니라 clock-in 시점.
    # 정확한 검증을 위해 attendance row 직접 만들기.
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models.attendance import Attendance
    from sqlalchemy import select

    async with async_session() as db:
        # 위 fixture 가 만든 schedule 의 work_date 와 store_id 조회
        from app.models.schedule import Schedule
        sched = (
            await db.execute(select(Schedule).where(Schedule.id == test_schedule))
        ).scalar_one()
        att = Attendance(
            organization_id=sched.organization_id,
            user_id=sched.user_id,
            store_id=sched.store_id,
            schedule_id=sched.id,
            work_date=sched.work_date,
            status="upcoming",
        )
        db.add(att)
        await db.commit()

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user["id"])
    # status 는 upcoming/soon/late/working 중 하나 (시각에 따라). None 아님.
    assert body["today_status"] is not None
    # Stage J: scheduled_end 가 schedule.end_time 으로부터 채워져야 함
    assert body["scheduled_end"] is not None
    # break 진행 중 아니므로 current_break 는 None
    assert body["current_break"] is None


# ── error path ────────────────────────────────────────────────


async def test_identify_by_pin_unknown_pin_returns_400(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    """매치되는 user 없는 PIN → 400 'Invalid PIN'."""
    # 모든 test user 의 PIN 과 다른 값
    used_pins = {info["clockin_pin"] for info in test_users.values()}
    candidate = "100000"
    while candidate in used_pins:
        candidate = f"{int(candidate) + 1:06d}"

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": candidate},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid PIN"


async def test_identify_by_pin_inactive_user_returns_400(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    db: AsyncSession,
) -> None:
    """user is_active=false → 400 (식별 X). 끝나면 원복."""
    user_id = str(test_user["id"])
    try:
        await db.execute(
            text("UPDATE users SET is_active=false WHERE id=:id"),
            {"id": user_id},
        )
        await db.commit()

        resp = await async_client.post(
            "/api/v1/attendance/identify-by-pin",
            headers=device_auth_headers,
            json={"pin": test_user["clockin_pin"]},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Invalid PIN"
    finally:
        await db.execute(
            text("UPDATE users SET is_active=true WHERE id=:id"),
            {"id": user_id},
        )
        await db.commit()


async def test_identify_by_pin_deleted_user_returns_400(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    db: AsyncSession,
) -> None:
    """user deleted_at IS NOT NULL → 400. 끝나면 원복."""
    user_id = str(test_user["id"])
    try:
        await db.execute(
            text("UPDATE users SET deleted_at=now() WHERE id=:id"),
            {"id": user_id},
        )
        await db.commit()

        resp = await async_client.post(
            "/api/v1/attendance/identify-by-pin",
            headers=device_auth_headers,
            json={"pin": test_user["clockin_pin"]},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Invalid PIN"
    finally:
        await db.execute(
            text("UPDATE users SET deleted_at=NULL WHERE id=:id"),
            {"id": user_id},
        )
        await db.commit()


@pytest.mark.parametrize("bad_pin", ["", "123", "1234567", "abcdef", "12abcd"])
async def test_identify_by_pin_invalid_format_returns_422(
    async_client: AsyncClient,
    device_auth_headers: dict,
    bad_pin: str,
) -> None:
    """PIN 형식 위반 (길이 4~6 외 / 비숫자) → 422 (Pydantic validation)."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": bad_pin},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("variable_pin", ["1234", "12345", "123456"])
async def test_identify_by_pin_accepts_4_to_6_digits(
    async_client: AsyncClient,
    device_auth_headers: dict,
    variable_pin: str,
) -> None:
    """Stage J: PIN 길이 4~6 모두 형식 통과 (등록된 user 가 없으면 400 'Invalid PIN')."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": variable_pin},
    )
    # 422 (형식 거부) 가 아니어야 함. 200 (매치) 또는 400 (등록 PIN 없음).
    assert resp.status_code in (200, 400), resp.text


async def test_identify_by_pin_no_device_token_returns_401(
    async_client: AsyncClient,
) -> None:
    """device token 없으면 401 (custom get_current_attendance_device)."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        json={"pin": "123456"},
    )
    assert resp.status_code == 401, resp.text


# ── edge: device 가 store 미할당 ──────────────────────────────


async def test_identify_by_pin_device_without_store_returns_null_status(
    async_client: AsyncClient,
    unassigned_device_token: str,
    test_user: dict,
) -> None:
    """device.store_id None → user 식별은 되지만 today_status=None."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers={"Authorization": f"Bearer {unassigned_device_token}"},
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user["id"])
    assert body["today_status"] is None
