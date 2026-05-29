"""Integration tests — Issue 4 (manage rename callsite hotfix).

Phase 6 (admin → manage 리네이밍) 머지 시 4개 호출처가 rename 안 되어 NameError 가
운영에서 터졌던 영역. 본 테스트가 회귀 방지.

검증 대상 (happy path):
  - POST  /api/v1/attendance/manage/schedules        (create — _manage_schedule_row 호출)
  - PATCH /api/v1/attendance/manage/schedules/{id}   (update — 동일)
  - POST  /api/v1/attendance/manage/clock cancel_clock_in   (_manage_cancel_clock_in)
  - POST  /api/v1/attendance/manage/clock cancel_clock_out  (_manage_cancel_clock_out)

Phase 6 결과 노트엔 manage 진입(PIN) 검증만 있고 실제 액션 검증은 누락된 결함을 보강.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def gm_user(test_users: dict) -> dict:
    """testgm 정보 반환 (PIN 포함)."""
    return test_users["testgm"]


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    """user_stores idempotent ensure."""
    async with async_session() as db:
        existing = (await db.execute(
            select(UserStore).where(
                UserStore.user_id == user_id,
                UserStore.store_id == store_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager))
        else:
            if is_manager and not existing.is_manager:
                existing.is_manager = True
        await db.commit()


@pytest_asyncio.fixture
async def gm_as_store_manager(gm_user: dict, test_store_id: UUID) -> None:
    """testgm 을 test_store_id 의 is_manager=True 로 등록."""
    await _ensure_user_store(gm_user["id"], test_store_id, is_manager=True)


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    """teststaff 를 test_store_id 에 assign — schedule 생성 검증 통과."""
    await _ensure_user_store(test_user["id"], test_store_id, is_manager=False)


@pytest_asyncio.fixture
async def manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    gm_user: dict,
    gm_as_store_manager: None,
) -> dict:
    """device Authorization + X-Manage-Session 두 헤더 합쳐 반환."""
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm_user["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["manage_token"]
    return {**device_auth_headers, "X-Manage-Session": token}


# ── POST /manage/schedules (create) — _manage_schedule_row ──────────


async def test_manage_create_schedule_returns_row(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """schedule 생성 endpoint 응답이 ManageScheduleRow 로 정상 직렬화 — NameError 회귀 방지."""
    resp = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "10:00",
            "end_time": "14:00",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user["id"])
    assert body["start_time"] == "10:00"


# ── PATCH /manage/schedules/{id} (update) — _manage_schedule_row ──


async def test_manage_update_schedule_returns_row(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """schedule 수정 endpoint 응답 정상 직렬화 — NameError 회귀 방지."""
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "11:00",
            "end_time": "15:00",
        },
    )
    assert create.status_code == 201, create.text
    sid = create.json()["schedule_id"]

    resp = await async_client.patch(
        f"/api/v1/attendance/manage/schedules/{sid}",
        headers=manage_headers,
        json={"start_time": "12:00"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_time"] == "12:00"
    assert body["end_time"] == "15:00"


# ── POST /manage/clock cancel_clock_in — _manage_cancel_clock_in ──


async def test_manage_cancel_clock_in_returns_ok(
    async_client: AsyncClient,
    manage_headers: dict,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """cancel_clock_in endpoint 응답 정상 — NameError 회귀 방지.

    먼저 schedule 만들고 clock-in 한 다음 cancel.
    """
    # schedule + clock-in
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": datetime.now(timezone.utc).strftime("%H:%M"),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%H:%M"),
        },
    )
    assert create.status_code == 201, create.text

    clock_in = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
        },
    )
    assert clock_in.status_code == 200, clock_in.text

    # cancel
    resp = await async_client.post(
        "/api/v1/attendance/manage/clock",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "action": "cancel_clock_in",
            "reason": "test cancellation",
        },
    )
    assert resp.status_code == 200, resp.text


# ── POST /manage/clock cancel_clock_out — _manage_cancel_clock_out ──


async def test_manage_cancel_clock_out_returns_ok(
    async_client: AsyncClient,
    manage_headers: dict,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """cancel_clock_out endpoint 응답 정상 — NameError 회귀 방지.

    schedule → clock-in → clock-out 한 다음 cancel.
    """
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": datetime.now(timezone.utc).strftime("%H:%M"),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%H:%M"),
        },
    )
    assert create.status_code == 201, create.text

    clock_in = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]},
    )
    assert clock_in.status_code == 200, clock_in.text

    clock_out = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "reason": "test early out",
        },
    )
    assert clock_out.status_code == 200, clock_out.text

    resp = await async_client.post(
        "/api/v1/attendance/manage/clock",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "action": "cancel_clock_out",
            "reason": "test cancellation",
        },
    )
    assert resp.status_code == 200, resp.text


# ── GET /manage/schedules — state / anomalies / breaks (Issue 10 Step 1) ──


async def test_manage_list_includes_state_anomalies_breaks(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """미출근(clock-in 전) 스케줄은 state=upcoming, breaks 빈 배열, anomalies 는 list."""
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": datetime.now(timezone.utc).strftime("%H:%M"),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%H:%M"),
        },
    )
    assert create.status_code == 201, create.text

    resp = await async_client.get("/api/v1/attendance/manage/schedules", headers=manage_headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    row = next(r for r in rows if r["user_id"] == str(test_user["id"]))
    assert row["state"] == "upcoming"
    assert isinstance(row["anomalies"], list)
    assert row["breaks"] == []


async def test_manage_list_breaking_state_with_breaks(
    async_client: AsyncClient,
    manage_headers: dict,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """clock-in → break_start 하면 state=breaking, breaks 에 진행 중(end=null) 1건."""
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": datetime.now(timezone.utc).strftime("%H:%M"),
            "end_time": (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%H:%M"),
        },
    )
    assert create.status_code == 201, create.text

    clock_in = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]},
    )
    assert clock_in.status_code == 200, clock_in.text

    brk = await async_client.post(
        "/api/v1/attendance/manage/clock",
        headers=manage_headers,
        json={"user_id": str(test_user["id"]), "action": "break_start", "break_type": "paid_10min"},
    )
    assert brk.status_code == 200, brk.text

    resp = await async_client.get("/api/v1/attendance/manage/schedules", headers=manage_headers)
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["user_id"] == str(test_user["id"]))
    assert row["state"] == "breaking"
    assert len(row["breaks"]) == 1
    b = row["breaks"][0]
    assert b["type"] == "paid_10min"
    assert b["end"] is None
    assert b["start"]  # "HH:mm"
