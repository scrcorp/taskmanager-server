"""새벽(자정~경계) 근무 전체 라이프사이클 e2e — 워크인 → 스케줄 수정 → 근태 시각 수정.

"스케줄 생성만 새벽이 적용되고 수정/근태에서 안 먹히면 문제" 검증.
시간 조작 없이 새벽을 재현: 매장 timezone을 "지금이 store-local 새벽(1~4시)"인
고정 오프셋 IANA 존(Etc/GMT±N)으로 잠시 설정 → 서버의 실제 now가 그 매장에선 새벽.

검증 항목:
  1. 새벽 워크인 clock-in → 스케줄 work_date=전날(영업일), start_at=오늘 달력일(실제 새벽)
  2. 키오스크(구 필드) 스케줄 시각 수정 → start_at의 +1d 날짜 보존
  3. 근태 시각 수정(clock_in_hhmm) → 영업일이 아닌 실제 달력일(operating day+1)로 합성
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as tz_utc
from typing import AsyncIterator
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.settings import StoreSetting
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

WALK_IN_KEY = "attendance.walk_in_allowed"


def _night_zone() -> str:
    """지금이 store-local 새벽(1~4시)이 되는 고정 오프셋 IANA 존을 고른다."""
    for n in range(-14, 13):  # Etc/GMT-14(=UTC+14) .. Etc/GMT+12(=UTC-12)
        name = f"Etc/GMT{'+' if n > 0 else ''}{n}" if n != 0 else "Etc/GMT"
        try:
            h = datetime.now(ZoneInfo(name)).hour
        except Exception:
            continue
        if 1 <= h <= 4:
            return name
    raise RuntimeError("no night zone found (unreachable)")


async def _set_setting(store_id: UUID, key: str, value) -> None:
    async with async_session() as db:
        row = (await db.execute(select(StoreSetting).where(
            StoreSetting.store_id == store_id, StoreSetting.key == key
        ))).scalar_one_or_none()
        if row is not None:
            row.value = value
        else:
            db.add(StoreSetting(store_id=store_id, key=key, value=value))
        await db.commit()


async def _clear_setting(store_id: UUID, key: str) -> None:
    async with async_session() as db:
        await db.execute(delete(StoreSetting).where(
            StoreSetting.store_id == store_id, StoreSetting.key == key
        ))
        await db.commit()


@pytest_asyncio.fixture
async def night_store(test_store_id: UUID) -> AsyncIterator[dict]:
    """매장 tz를 '지금이 새벽인 존'으로 + 워크인 허용. teardown에서 복원."""
    zone = _night_zone()
    # 설정 레지스트리 시드 — walk_in_allowed 키가 등록돼 있어야 resolve됨
    from app.main import seed_settings_registry
    await seed_settings_registry()
    async with async_session() as db:
        store = (await db.execute(select(Store).where(Store.id == test_store_id))).scalar_one()
        orig_tz, orig_ds = store.timezone, store.day_start_time
        await db.execute(update(Store).where(Store.id == test_store_id)
                         .values(timezone=zone, day_start_time=None))
        await db.commit()
    await _set_setting(test_store_id, WALK_IN_KEY, True)
    sl_now = datetime.now(ZoneInfo(zone))
    info = {
        "zone": zone,
        "sl_today": sl_now.date(),            # 실제 달력일 (새벽이 속한 날)
        "operating_day": sl_now.date() - timedelta(days=1),  # 영업일 (경계 06:00 이전)
    }
    try:
        yield info
    finally:
        async with async_session() as db:
            await db.execute(update(Store).where(Store.id == test_store_id)
                             .values(timezone=orig_tz, day_start_time=orig_ds))
            await db.commit()
        await _clear_setting(test_store_id, WALK_IN_KEY)


@pytest_asyncio.fixture
async def _night_cleanup(test_user: dict, night_store: dict) -> AsyncIterator[None]:
    """테스트가 만든 근태/스케줄 정리 (영업일·실제일 양쪽)."""
    days = [night_store["operating_day"], night_store["sl_today"]]
    yield
    async with async_session() as db:
        await db.execute(delete(Attendance).where(
            Attendance.user_id == test_user["id"], Attendance.work_date.in_(days)))
        await db.execute(delete(Schedule).where(
            Schedule.user_id == test_user["id"], Schedule.work_date.in_(days)))
        await db.commit()


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    async with async_session() as db:
        exists = (await db.execute(select(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id
        ))).scalar_one_or_none()
        if exists is None:
            db.add(UserStore(user_id=test_user["id"], store_id=test_store_id, is_manager=False))
            await db.commit()


@pytest_asyncio.fixture
async def manage_headers(
    async_client: AsyncClient, device_auth_headers: dict, test_users: dict, test_store_id: UUID,
) -> dict:
    gm = test_users["testgm"]
    async with async_session() as db:
        exists = (await db.execute(select(UserStore).where(
            UserStore.user_id == gm["id"], UserStore.store_id == test_store_id
        ))).scalar_one_or_none()
        if exists is None:
            db.add(UserStore(user_id=gm["id"], store_id=test_store_id, is_manager=True))
        else:
            exists.is_manager = True
        await db.commit()
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def test_early_morning_walkin_full_lifecycle(
    async_client: AsyncClient,
    device_auth_headers: dict,
    manage_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    night_store: dict,
    staff_in_store: None,
    _night_cleanup: None,
):
    op_day: date = night_store["operating_day"]
    sl_today: date = night_store["sl_today"]
    zone = ZoneInfo(night_store["zone"])

    # ── 1) 새벽 워크인 clock-in ─────────────────────────────────────
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"], "walk_in": True},
    )
    assert resp.status_code == 200, resp.text

    async with async_session() as db:
        sched = (await db.execute(select(Schedule).where(
            Schedule.user_id == test_user["id"],
            Schedule.store_id == test_store_id,
            Schedule.origin == "walk_in",
            Schedule.work_date == op_day,
        ))).scalar_one_or_none()
    assert sched is not None, "새벽 워크인 스케줄이 영업일(전날)로 귀속되어야 함"
    # 실제 시각은 오늘 달력일 새벽이어야 함 (영업일로 당겨지면 안 됨)
    assert sched.start_at is not None
    assert sched.start_at.date() == sl_today, (
        f"start_at={sched.start_at} 이 실제 달력일({sl_today})이어야 하는데 영업일로 오귀속"
    )
    sched_id = sched.id

    # ── 2) 키오스크(구 필드) 스케줄 시각 수정 → +1d 보존 ──────────────
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/schedules/{sched_id}",
        headers=manage_headers,
        json={"start_time": "03:00", "end_time": "09:00"},
    )
    assert resp.status_code == 200, resp.text
    async with async_session() as db:
        sched2 = (await db.execute(select(Schedule).where(Schedule.id == sched_id))).scalar_one()
    assert sched2.start_at.date() == sl_today, (
        f"구 필드 수정 후 start_at={sched2.start_at} — +1d가 소실되어 영업일로 당겨짐"
    )
    assert sched2.start_at.strftime("%H:%M") == "03:00"
    assert sched2.work_date == op_day  # 영업일 라벨 유지

    # ── 3) 근태 시각 수정(clock_in_hhmm) → 실제 달력일로 합성 ─────────
    now_sl = datetime.now(zone)
    hhmm = (now_sl - timedelta(minutes=30)).strftime("%H:%M")
    resp = await async_client.post(
        "/api/v1/attendance/manage/attendance/status",
        headers=manage_headers,
        json={"user_id": str(test_user["id"]), "status": "working",
              "clock_in_hhmm": hhmm, "reason": "early morning correction test"},
    )
    assert resp.status_code == 200, resp.text
    async with async_session() as db:
        att = (await db.execute(select(Attendance).where(
            Attendance.user_id == test_user["id"], Attendance.work_date == op_day,
            Attendance.store_id == test_store_id,
        ))).scalar_one()
    assert att.clock_in is not None
    corrected_local = att.clock_in.astimezone(zone)
    assert corrected_local.date() == sl_today, (
        f"근태 시각 수정이 영업일({op_day})로 합성됨 — 실제 달력일({sl_today})이어야 함:"
        f" clock_in={corrected_local.isoformat()}"
    )
    assert corrected_local.strftime("%H:%M") == hhmm


async def test_kiosk_dawn_create_anchors_next_calendar_day(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    night_store: dict,
    staff_in_store: None,
    _night_cleanup: None,
):
    """키오스크(HHmm만 입력 가능)로 새벽조 생성 — 영업일 당일(과거)로 앵커되어
    즉시 no_show가 되던 구멍 수정 검증: 경계 이전 시각은 익일 달력일로 번역."""
    op_day: date = night_store["operating_day"]
    sl_today: date = night_store["sl_today"]

    resp = await async_client.post(
        "/api/v1/attendance/manage/schedules",
        headers=manage_headers,
        json={"user_id": str(test_user["id"]), "start_time": "04:30", "end_time": "05:30"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operating_day"] == op_day.isoformat()      # 영업일 라벨
    assert body["start_at"] == f"{sl_today.isoformat()}T04:30", body  # 실제 달력일(익일)

    async with async_session() as db:
        sched = (await db.execute(select(Schedule).where(
            Schedule.user_id == test_user["id"], Schedule.work_date == op_day,
            Schedule.store_id == test_store_id, Schedule.origin == "manual",
        ))).scalar_one()
        assert sched.start_at.date() == sl_today
        att = (await db.execute(select(Attendance).where(
            Attendance.schedule_id == sched.id
        ))).scalar_one_or_none()
    # 시작이 미래(새벽 4:30, 지금은 1~4시 사이) → no_show가 아니어야 함
    assert att is not None and att.status in ("upcoming", "late"), att.status
