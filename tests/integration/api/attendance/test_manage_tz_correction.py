"""Integration tests — AK-1 (수동 clock 시각 보정 타임존 해석).

수동 시각 입력이 항상 **매장 타임존** 벽시계로 해석되어 UTC instant 로
저장되는지 검증. 디바이스/브라우저/서버 로컬 타임존이 어긋나도 payroll
시간이 밀리지 않아야 한다.

검증 대상:
  - POST  /api/v1/attendance/manage/attendance/status (kiosk manage,
    clock_in_hhmm/clock_out_hhmm — store tz 해석 계약 pinning)
  - PATCH /api/v1/console/attendances/{id}/correct (console 수동 정정,
    naive ISO → store tz 해석 / offset 명시 → 존중 / invalid → 400)
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio

LA_TZ = "America/Los_Angeles"
LA_STORE_NAME = "__attendance_tz_store_LA__"


# ── helpers / fixtures ────────────────────────────────────────────────


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    """user_stores idempotent ensure (test_manage_endpoints 와 동일 패턴)."""
    async with async_session() as db:
        existing = (await db.execute(
            select(UserStore).where(
                UserStore.user_id == user_id,
                UserStore.store_id == store_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager))
        elif is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()


async def _purge_store_rows(store_id: UUID) -> None:
    """LA 매장의 attendance/schedule 정리 — conftest 의 purge 는 이 매장을 모른다."""
    async with async_session() as db:
        await db.execute(delete(Attendance).where(Attendance.store_id == store_id))
        await db.execute(delete(Schedule).where(Schedule.store_id == store_id))
        await db.commit()


@pytest_asyncio.fixture
async def la_store_id(seed_organization: dict):
    """LA 타임존 매장 ensure + 전후 데이터 정리."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        store = (await db.execute(
            select(Store).where(
                Store.organization_id == org_id,
                Store.name == LA_STORE_NAME,
            )
        )).scalar_one_or_none()
        if store is None:
            store = Store(
                organization_id=org_id,
                name=LA_STORE_NAME,
                timezone=LA_TZ,
                day_start_time={"all": "00:00"},
            )
            db.add(store)
            await db.commit()
            await db.refresh(store)
        else:
            store.timezone = LA_TZ
            store.day_start_time = {"all": "00:00"}
            store.deleted_at = None
            store.status = "open"
            await db.commit()
        sid = store.id
    await _purge_store_rows(sid)
    yield sid
    await _purge_store_rows(sid)


@pytest_asyncio.fixture
async def la_device_headers(
    async_client: AsyncClient,
    attendance_access_code: str,
    la_store_id: UUID,
    _session_created_device_ids: list,
) -> dict:
    """LA 매장에 할당된 kiosk 디바이스 헤더."""
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code, "fingerprint": "pytest-la-tz"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _session_created_device_ids.append(UUID(body["device_id"]))
    token = body["token"]
    resp2 = await async_client.put(
        "/api/v1/attendance/store",
        headers={"Authorization": f"Bearer {token}"},
        json={"store_id": str(la_store_id)},
    )
    assert resp2.status_code == 200, resp2.text
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def la_manage_headers(
    async_client: AsyncClient,
    la_device_headers: dict,
    la_store_id: UUID,
    test_users: dict,
) -> dict:
    """testgm 을 LA 매장 매니저로 등록 후 manage session 헤더 반환."""
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], la_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=la_device_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**la_device_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def utc_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    test_users: dict,
) -> dict:
    """기존 UTC 테스트 매장용 manage session 헤더 (same-tz 회귀 경로)."""
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def _seed_schedule_and_attendance(
    user_info: dict,
    store_id: UUID,
    *,
    status: str = "upcoming",
    clock_in: datetime | None = None,
) -> tuple[UUID, UUID, date]:
    """오늘(매장 영업일) confirmed 스케줄 + eager attendance row 를 시드.

    Returns: (schedule_id, attendance_id, operating_day)
    """
    from app.utils.timezone import get_store_day_config, get_work_date

    async with async_session() as db:
        tz_name, day_cfg = await get_store_day_config(db, store_id)
        today = get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))
        sched = Schedule(
            organization_id=user_info["organization_id"],
            user_id=user_info["id"],
            store_id=store_id,
            operating_day=today,
            start_at=datetime.combine(today, time(9, 0)),   # 벽시계 naive 계약
            end_at=datetime.combine(today, time(17, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=user_info["organization_id"],
            store_id=store_id,
            user_id=user_info["id"],
            schedule_id=sched.id,
            work_date=today,
            status=status,
            clock_in=clock_in,
            clock_in_timezone=tz_name if clock_in is not None else None,
        )
        db.add(att)
        await db.commit()
        return sched.id, att.id, today


async def _fetch_attendance(attendance_id: UUID) -> Attendance:
    async with async_session() as db:
        return (await db.execute(
            select(Attendance).where(Attendance.id == attendance_id)
        )).scalar_one()


def _expected_utc(day: date, hhmm: time, tz_name: str) -> datetime:
    """매장 tz 벽시계 → 기대 UTC instant."""
    return datetime.combine(day, hhmm, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


# ── kiosk manage — /manage/attendance/status ─────────────────────────


async def test_manage_status_hhmm_interpreted_in_store_tz(
    async_client: AsyncClient,
    la_manage_headers: dict,
    la_store_id: UUID,
    test_user: dict,
) -> None:
    """매장 tz(LA)와 디바이스/서버 로컬 tz 가 달라도 HH:mm 은 매장 tz 로 해석.

    저장된 UTC instant == LA 벽시계 해석이어야 한다 (UTC/서버 로컬 오독이면 실패).
    """
    _sid, att_id, today = await _seed_schedule_and_attendance(test_user, la_store_id)

    resp = await async_client.post(
        "/api/v1/attendance/manage/attendance/status",
        headers=la_manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "status": "clocked_out",
            "clock_in_hhmm": "09:00",
            "clock_out_hhmm": "17:00",
            "reason": "tz interpretation test",
        },
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch_attendance(att_id)
    exp_in = _expected_utc(today, time(9, 0), LA_TZ)
    exp_out = _expected_utc(today, time(17, 0), LA_TZ)
    assert att.clock_in is not None and att.clock_out is not None
    assert att.clock_in.astimezone(timezone.utc) == exp_in
    assert att.clock_out.astimezone(timezone.utc) == exp_out
    # tz-name snapshot 은 매장 tz
    assert att.clock_in_timezone == LA_TZ
    assert att.clock_out_timezone == LA_TZ
    assert att.total_work_minutes == 480
    # LA 해석은 UTC 벽시계 해석과 다른 instant — 오독 회귀 시 위 assert 가 잡는다
    assert exp_in != datetime.combine(today, time(9, 0), tzinfo=timezone.utc)


async def test_manage_status_hhmm_same_tz_regression(
    async_client: AsyncClient,
    utc_manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """same-tz(UTC 매장) 정상 경로 회귀 — HH:mm 이 그대로 UTC instant."""
    _sid, att_id, today = await _seed_schedule_and_attendance(test_user, test_store_id)

    resp = await async_client.post(
        "/api/v1/attendance/manage/attendance/status",
        headers=utc_manage_headers,
        json={
            "user_id": str(test_user["id"]),
            "status": "clocked_out",
            "clock_in_hhmm": "09:00",
            "clock_out_hhmm": "17:30",
            "reason": "same tz regression",
        },
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch_attendance(att_id)
    assert att.clock_in.astimezone(timezone.utc) == datetime.combine(
        today, time(9, 0), tzinfo=timezone.utc
    )
    assert att.clock_out.astimezone(timezone.utc) == datetime.combine(
        today, time(17, 30), tzinfo=timezone.utc
    )
    assert att.clock_in_timezone == "UTC"
    assert att.clock_out_timezone == "UTC"
    assert att.total_work_minutes == 510


# ── console — /attendances/{id}/correct ───────────────────────────────


async def test_console_correct_naive_value_uses_store_tz(
    async_client: AsyncClient,
    admin_headers: dict,
    la_store_id: UUID,
    test_user: dict,
) -> None:
    """naive ISO corrected_value 는 매장 tz(LA) 벽시계로 해석되어 UTC 저장.

    이전 버그: naive 가 그대로 TIMESTAMPTZ 에 저장돼 UTC 로 오기록 (LA 기준
    7~8시간 밀림 → payroll 오염).
    """
    # working 상태로 시드하되 clock_in 은 correction 으로 세팅한다
    _sid, att_id, today = await _seed_schedule_and_attendance(
        test_user, la_store_id, status="working",
    )

    resp = await async_client.patch(
        f"/api/v1/console/attendances/{att_id}/correct",
        headers=admin_headers,
        json={
            "field_name": "clock_in",
            "corrected_value": f"{today.isoformat()}T09:00:00",  # naive
            "reason": "naive tz test",
        },
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch_attendance(att_id)
    exp_in = _expected_utc(today, time(9, 0), LA_TZ)
    assert att.clock_in.astimezone(timezone.utc) == exp_in
    assert att.clock_in_timezone == LA_TZ
    # audit trail 의 corrected_value 도 실제 저장 instant 와 일치 (UTC ISO)
    assert resp.json()["corrected_value"] == exp_in.isoformat()


async def test_console_correct_explicit_offset_respected(
    async_client: AsyncClient,
    admin_headers: dict,
    la_store_id: UUID,
    test_user: dict,
) -> None:
    """offset 명시(UTC Z 등) corrected_value 는 instant 그대로 저장 + 재계산 정상."""
    _sid, att_id, today = await _seed_schedule_and_attendance(
        test_user, la_store_id, status="working",
    )
    exp_in = _expected_utc(today, time(9, 0), LA_TZ)
    exp_out = _expected_utc(today, time(17, 0), LA_TZ)

    r1 = await async_client.patch(
        f"/api/v1/console/attendances/{att_id}/correct",
        headers=admin_headers,
        json={
            "field_name": "clock_in",
            "corrected_value": exp_in.isoformat(),  # aware UTC — instant 유지돼야 함
            "reason": "aware in",
        },
    )
    assert r1.status_code == 200, r1.text
    r2 = await async_client.patch(
        f"/api/v1/console/attendances/{att_id}/correct",
        headers=admin_headers,
        json={
            "field_name": "clock_out",
            "corrected_value": f"{today.isoformat()}T17:00:00",  # naive LA 벽시계
            "reason": "naive out",
        },
    )
    assert r2.status_code == 200, r2.text

    att = await _fetch_attendance(att_id)
    assert att.clock_in.astimezone(timezone.utc) == exp_in
    assert att.clock_out.astimezone(timezone.utc) == exp_out
    assert att.clock_out_timezone == LA_TZ
    # naive + aware 혼합이던 경로 — 이제 둘 다 aware UTC 라 재계산도 안전
    assert att.total_work_minutes == 480


async def test_console_correct_invalid_datetime_returns_400(
    async_client: AsyncClient,
    admin_headers: dict,
    la_store_id: UUID,
    test_user: dict,
) -> None:
    """비-ISO 문자열은 500 이 아니라 400 + 명확한 메시지."""
    _sid, att_id, _today = await _seed_schedule_and_attendance(
        test_user, la_store_id, status="working",
    )
    resp = await async_client.patch(
        f"/api/v1/console/attendances/{att_id}/correct",
        headers=admin_headers,
        json={
            "field_name": "clock_in",
            "corrected_value": "not-a-datetime",
            "reason": "invalid input",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "Invalid datetime" in resp.text
