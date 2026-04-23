"""Attendance Device — today-staff / stores / notices 대시보드 응답 테스트."""

from __future__ import annotations

from datetime import time
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.communication import Announcement


pytestmark = pytest.mark.asyncio


# ── today-staff ────────────────────────────────────────────────────────


async def test_today_staff_empty_when_no_schedules(
    async_client: AsyncClient,
    device_auth_headers: dict,
) -> None:
    resp = await async_client.get(
        "/api/v1/attendance/today-staff", headers=device_auth_headers
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_today_staff_returns_scheduled_users(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    await make_schedule(test_users["teststaff"], start_time=time(8, 0), end_time=time(16, 0))
    await make_schedule(test_users["testsv"], start_time=time(10, 0), end_time=time(18, 0))

    resp = await async_client.get(
        "/api/v1/attendance/today-staff", headers=device_auth_headers
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    user_ids = {r["user_id"] for r in rows}
    assert str(test_users["teststaff"]["id"]) in user_ids
    assert str(test_users["testsv"]["id"]) in user_ids
    for row in rows:
        assert row["status"] == "not_yet"


async def test_today_staff_status_transitions(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    pin = test_user["clockin_pin"]
    uid = str(test_user["id"])

    # clock in → status should become 'working'
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )
    rows = (await async_client.get("/api/v1/attendance/today-staff", headers=device_auth_headers)).json()
    me = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert me["status"] == "working"

    # break-start → on_break + current_break populated
    await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={"pin": pin, "user_id": uid, "break_type": "paid_short"},
    )
    rows = (await async_client.get("/api/v1/attendance/today-staff", headers=device_auth_headers)).json()
    me = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert me["status"] == "on_break"
    assert me["current_break"] is not None
    assert me["current_break"]["break_type"] == "paid_short"

    # break-end → working
    await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )
    rows = (await async_client.get("/api/v1/attendance/today-staff", headers=device_auth_headers)).json()
    me = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert me["status"] == "working"
    assert me["current_break"] is None

    # clock-out → clocked_out
    await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )
    rows = (await async_client.get("/api/v1/attendance/today-staff", headers=device_auth_headers)).json()
    me = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert me["status"] == "clocked_out"


async def test_today_staff_aggregates_break_minutes(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_schedule: UUID,
) -> None:
    pin = test_user["clockin_pin"]
    uid = str(test_user["id"])
    await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )

    # 유급 휴식
    await async_client.post(
        "/api/v1/attendance/break-start", headers=device_auth_headers,
        json={"pin": pin, "user_id": uid, "break_type": "paid_short"},
    )
    await async_client.post(
        "/api/v1/attendance/break-end", headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )
    # 무급 휴식
    await async_client.post(
        "/api/v1/attendance/break-start", headers=device_auth_headers,
        json={"pin": pin, "user_id": uid, "break_type": "unpaid_long"},
    )
    await async_client.post(
        "/api/v1/attendance/break-end", headers=device_auth_headers,
        json={"pin": pin, "user_id": uid},
    )

    rows = (await async_client.get("/api/v1/attendance/today-staff", headers=device_auth_headers)).json()
    me = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert me["paid_break_minutes"] >= 0
    assert me["unpaid_break_minutes"] >= 0
    # 두 휴식 모두 종료됨 → current_break None
    assert me["current_break"] is None


# ── stores ─────────────────────────────────────────────────────────────


async def test_stores_endpoint_returns_org_stores(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    resp = await async_client.get(
        "/api/v1/attendance/stores", headers=device_auth_headers
    )
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["id"] for r in rows}
    assert str(test_store_id) in ids
    assert str(second_store_id) in ids


# ── notices ────────────────────────────────────────────────────────────


async def test_notices_returns_org_and_store_scoped(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    org_id = test_users["testadmin"]["organization_id"]

    async with async_session() as db:
        org_wide = Announcement(
            organization_id=org_id,
            store_id=None,
            title="__TEST__ org wide",
            content="hello org",
            created_by=test_users["testadmin"]["id"],
        )
        this_store = Announcement(
            organization_id=org_id,
            store_id=test_store_id,
            title="__TEST__ this store",
            content="hello store",
            created_by=test_users["testadmin"]["id"],
        )
        other_store = Announcement(
            organization_id=org_id,
            store_id=second_store_id,
            title="__TEST__ other store",
            content="not for this device",
            created_by=test_users["testadmin"]["id"],
        )
        db.add_all([org_wide, this_store, other_store])
        await db.commit()

    resp = await async_client.get(
        "/api/v1/attendance/notices", headers=device_auth_headers
    )
    assert resp.status_code == 200
    titles = {n["title"] for n in resp.json()}
    assert "__TEST__ org wide" in titles
    assert "__TEST__ this store" in titles
    assert "__TEST__ other store" not in titles
