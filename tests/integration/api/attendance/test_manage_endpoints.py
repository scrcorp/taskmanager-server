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
    """5분 grid 벗어난 시간은 400 으로 거부.

    D6-4: 판정은 서비스 단일 관문에서만 한다. 예전엔 스키마 validator 가
    먼저 걸어 422 가 나갔는데, 같은 위반이 두 형태로 나가면 클라가 양쪽을
    처리해야 하므로 스키마 validator 를 제거했다.
    """
    resp = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "10:17",  # off-grid
            "end_time": "14:00",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "5-minute increments" in resp.json()["detail"]


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
    assert resp.status_code == 400, resp.text
    assert "5-minute increments" in resp.json()["detail"]


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


async def test_console_schedule_now_accepts_5min_grid(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """D6: 콘솔도 5분 단위. 예전엔 30분이라 10:15 가 거부됐다."""
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
    assert resp.status_code == 201, resp.text


async def test_console_schedule_rejects_off_5min_grid(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """단위는 5분 하나 — 콘솔도 같은 문구로 거부한다."""
    resp = await async_client.post(
        "/api/v1/console/schedules",
        headers=admin_headers,
        json={
            "user_id": str(test_user["id"]),
            "store_id": str(test_store_id),
            "work_date": (date.today() + timedelta(days=31)).isoformat(),
            "start_time": "10:17",
            "end_time": "18:00",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "5-minute increments" in resp.json()["detail"]


async def test_partial_update_moves_start_to_dawn_with_correct_date(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    staff_in_store: None,
) -> None:
    """시작만 새벽 시각으로 바꾸면 달력일이 +1d 로 따라와야 한다 (D7-3 회귀).

    예전엔 한쪽 시각만 오면 키오스크의 영업일 번역이 통째로 스킵되고
    구 인코딩 조립이 기존 offset(0)으로 앵커해서, 새벽조가 **이미 지난 당일 02:00**
    으로 저장돼 즉시 no_show 가 됐다. 이제 서버가 기존 종료 시각과 병합해
    번역하므로 경계 규칙이 정상 적용된다.
    """
    # 테스트 매장 기본 경계는 00:00 이라 새벽 개념이 성립하지 않는다 — 06:00 으로 세팅.
    # **반드시 원복한다** — 경계는 work_date 계산에 쓰이므로 남겨두면 뒤따르는
    # 테스트의 no_show/late 판정을 통째로 바꿔버린다(실제로 겪음).
    async def _set_boundary(value: str) -> None:
        async with async_session() as db:
            await db.execute(text(
                "UPDATE stores SET day_start_time = CAST(:v AS jsonb)"
            ), {"v": value})
            await db.commit()

    await _set_boundary('{"all": "06:00"}')
    try:
        await _run_dawn_partial_update_case(async_client, manage_headers, test_user)
    finally:
        await _set_boundary('{"all": "00:00"}')


async def _run_dawn_partial_update_case(
    async_client: AsyncClient, manage_headers: dict, test_user: dict,
) -> None:
    create = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "start_time": "08:00",   # 경계 이후 → 영업일과 같은 달력일
            "end_time": "13:00",
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    sid = body["schedule_id"]
    operating_day = date.fromisoformat(body["operating_day"])
    assert body["start_at"][:10] == operating_day.isoformat(), body

    # 시작만 새벽으로 변경 — 종료는 보내지 않는다 (부분 수정)
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/schedules/{sid}",
        headers=manage_headers,
        json={"start_time": "02:00"},
    )
    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["start_at"][11:16] == "02:00", after
    # 핵심: 경계(06:00) 이전이므로 달력일이 영업일 +1 이어야 한다
    assert after["start_at"][:10] == (operating_day + timedelta(days=1)).isoformat(), after
    assert after["operating_day"] == operating_day.isoformat(), after
