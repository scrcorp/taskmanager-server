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

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio


def _grid_now_window(hours: int = 4) -> tuple[str, str]:
    """현재 시각(UTC=테스트 매장 tz)을 30분 grid 로 내림한 (start, end) HH:MM 쌍.

    manage schedule create 는 30분 grid(:00/:30) 만 허용하므로 now() 를 그대로
    보내면 시각에 따라 422 가 난다 (flaky). 직전 grid 로 내림 — 스케줄이 현재
    시각을 포함하므로 clock-in 가능하고, end 전이라 state 판정도 동일.
    """
    now = datetime.now(timezone.utc)
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    end = start + timedelta(hours=hours)
    return start.strftime("%H:%M"), end.strftime("%H:%M")


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


async def test_manage_create_schedule_rejects_off_5min_grid(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """5분 grid 벗어난 시간은 422 로 거부 (키오스크 step = 5분)."""
    resp = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "10:17",  # off-grid
            "end_time": "14:00",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_manage_create_schedule_accepts_5min_grid(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """콘솔(30분)과 달리 키오스크는 :15 같은 5분 단위를 허용한다."""
    resp = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "10:15",
            "end_time": "14:45",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["start_time"] == "10:15"
    assert body["end_time"] == "14:45"


async def test_manage_update_schedule_accepts_5min_grid(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """수정도 5분 단위 허용 — start_at/end_at 재조립까지 통과해야 한다."""
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
        json={
            "user_id": str(test_user["id"]),
            "start_time": "11:35",
            "end_time": "15:05",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_time"] == "11:35"
    assert body["end_time"] == "15:05"


async def test_manage_update_schedule_rejects_off_5min_grid(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """5분 grid 를 벗어난 수정은 여전히 거부."""
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
        json={"start_time": "11:07", "end_time": "15:00"},
    )
    assert resp.status_code == 422, resp.text


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
    start_hhmm, end_hhmm = _grid_now_window()
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": start_hhmm,
            "end_time": end_hhmm,
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
    start_hhmm, end_hhmm = _grid_now_window()
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": start_hhmm,
            "end_time": end_hhmm,
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
    start_hhmm, end_hhmm = _grid_now_window()
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": start_hhmm,
            "end_time": end_hhmm,
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
    start_hhmm, end_hhmm = _grid_now_window()
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": start_hhmm,
            "end_time": end_hhmm,
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


# ── console 회귀 — 키오스크 5분 완화가 console 로 새면 안 된다 ──


async def test_console_schedule_still_rejects_off_30min_grid(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """console 은 그대로 30분 grid. 키오스크가 허용하는 10:15 가 여기선 거부돼야 한다."""
    resp = await async_client.post(
        "/api/v1/console/schedules",
        headers=admin_headers,
        json={
            "user_id": str(test_user["id"]),
            "store_id": str(test_store_id),
            "work_date": (date.today() + timedelta(days=30)).isoformat(),
            "start_time": "10:15",
            "end_time": "18:00",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "hour or half-hour" in resp.json()["detail"]


# ── SV 매니저 권한 — 승인 워크플로 OFF(기본)면 confirmed 스케줄도 직접 관리 ──


@pytest_asyncio.fixture
async def sv_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> dict:
    """SV(testsv) 로 매니저 세션 오픈 — 매니저모드는 SV+ 부터 허용."""
    sv = test_users["testsv"]
    await _ensure_user_store(sv["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": sv["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def _set_approval_required(store_id: UUID, value: bool) -> None:
    """store 레벨 override 로 승인 워크플로 on/off."""
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO store_settings (id, store_id, key, value, updated_at) "
                "VALUES (gen_random_uuid(), :sid, 'schedule.approval_required', CAST(:v AS jsonb), now()) "
                "ON CONFLICT (store_id, key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"sid": str(store_id), "v": "true" if value else "false"},
        )
        await db.commit()


async def _clear_approval_setting(store_id: UUID) -> None:
    async with async_session() as db:
        await db.execute(
            text("DELETE FROM store_settings WHERE store_id = :sid AND key = 'schedule.approval_required'"),
            {"sid": str(store_id)},
        )
        await db.commit()


async def test_sv_manager_can_create_and_update_confirmed_schedule(
    async_client: AsyncClient,
    sv_manage_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """기본값(승인 워크플로 OFF): SV 도 confirmed 스케줄을 만들고 고칠 수 있다."""
    await _clear_approval_setting(test_store_id)
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=sv_manage_headers,
        json={"user_id": str(test_user["id"]), "start_time": "11:00", "end_time": "15:00"},
    )
    assert create.status_code == 201, create.text
    assert create.json()["status"] == "confirmed"
    sid = create.json()["schedule_id"]

    resp = await async_client.patch(
        f"/api/v1/attendance/manage/schedules/{sid}",
        headers=sv_manage_headers,
        json={"start_time": "11:35", "end_time": "15:05"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["start_time"] == "11:35"

    delete = await async_client.delete(
        f"/api/v1/attendance/manage/schedules/{sid}", headers=sv_manage_headers,
    )
    assert delete.status_code == 204, delete.text


async def test_sv_manager_blocked_when_approval_required_on(
    async_client: AsyncClient,
    manage_headers: dict,
    sv_manage_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """승인 워크플로 ON 인 매장에서는 SV 의 confirmed 스케줄 수정이 다시 막힌다."""
    # GM 으로 confirmed 스케줄 생성 (SV 생성은 requested 로 떨어지므로)
    await _clear_approval_setting(test_store_id)
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={"user_id": str(test_user["id"]), "start_time": "12:00", "end_time": "16:00"},
    )
    assert create.status_code == 201, create.text
    sid = create.json()["schedule_id"]

    await _set_approval_required(test_store_id, True)
    try:
        resp = await async_client.patch(
            f"/api/v1/attendance/manage/schedules/{sid}",
            headers=sv_manage_headers,
            json={"start_time": "12:30", "end_time": "16:00"},
        )
        assert resp.status_code == 403, resp.text
        assert "GM or above" in resp.json()["detail"]
    finally:
        await _clear_approval_setting(test_store_id)
