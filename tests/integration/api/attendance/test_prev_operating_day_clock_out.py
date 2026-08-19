"""★ 영업일이 넘어간 직후, **어제 영업일의 새벽조**로 퇴근이 되는가 (끝까지).

실제 매장 설정을 숫자 그대로 재현한다:

    1. 매장 영업일 경계  day_start = 04:00
    2. 지금                8/18 05:00        → 오늘 영업일 = 8/18 (05:00 ≥ 04:00)
    3. 근무                영업일 8/17 의 새벽조, 실제 시각 8/18 01:00 ~ 06:00
                          (01:00 은 경계 이전이라 달력일 = 영업일 + 1일)
    4. 화면                HTMA 목록은 영업일 기준이라 "8/18" 을 보고 있다
    5. 직원이 PIN 입력

걱정: 서버가 "오늘(8/17 아님, 8/18)" 것만 들고 오면 이 근무가 안 잡혀서
Clock Out 을 띄울 근거(`today_status`)가 사라진다 → 키오스크에서 퇴근 불가.

`now` 를 고정할 수 없으므로 **매장 타임존을 "지금이 현지 05시인 존"으로 잡고 경계를 04:00**
으로 둔다. 그러면 위 숫자가 그대로 성립한다 — 경계를 상대값(now-1h)으로 옮기는 방식은
실행 시각이 자정 근처면 경계가 00:00 으로 떨어져 `+1일` 인코딩 자체가 안 만들어졌다.

이 파일이 고정하는 것은 **서버 응답**이다. 그 응답을 받은 앱이 실제로 Clock Out 을 여는지는
app 저장소의 `test/widget/prev_operating_day_clock_out_test.dart` 가 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

IDENTIFY_URL = "/api/v1/attendance/identify-by-pin"
CLOCK_OUT_URL = "/api/v1/attendance/clock-out"


def _zone_where_now_is(hour: int) -> str:
    """지금이 store-local `hour` 시대인 고정 오프셋 IANA 존. (`_night_zone` 과 같은 수법)"""
    for n in range(-14, 13):
        name = f"Etc/GMT{'+' if n > 0 else ''}{n}" if n != 0 else "Etc/GMT"
        try:
            if datetime.now(ZoneInfo(name)).hour == hour:
                return name
        except Exception:
            continue
    raise RuntimeError(f"no zone where local hour == {hour}")


@pytest_asyncio.fixture
async def store_at_five_am(test_user: dict, test_store_id: UUID) -> AsyncIterator[str]:
    """매장을 "경계 04:00 · 지금 현지 05시" 로 만든다 = 영업일이 방금 넘어간 상태."""
    zone = _zone_where_now_is(5)
    boundary = "04:00"
    async with async_session() as db:
        store = await db.get(Store, test_store_id)
        original = store.day_start_time
        original_tz = store.timezone
        store.day_start_time = {"all": boundary}
        store.timezone = zone
        if await db.scalar(
            select(UserStore).where(
                UserStore.user_id == test_user["id"],
                UserStore.store_id == test_store_id,
            )
        ) is None:
            db.add(UserStore(user_id=test_user["id"], store_id=test_store_id))
        await db.commit()
    try:
        yield boundary
    finally:
        async with async_session() as db:
            store = await db.get(Store, test_store_id)
            store.day_start_time = original
            store.timezone = original_tz
            await db.commit()


async def _store_tz(store_id: UUID) -> str:
    from app.utils.timezone import get_store_day_config

    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, store_id)
    return tz_name


async def test_dawn_shift_from_previous_operating_day_survives_the_rollover(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    store_at_five_am: str,
) -> None:
    from app.utils.timezone import get_store_day_config, get_work_date

    tz_name = await _store_tz(test_store_id)
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo(tz_name)).replace(second=0, microsecond=0, tzinfo=None)

    # 경계 1시간 전에 시작해 아직 안 끝난 근무 = 어제 영업일 라벨의 새벽조.
    start_local = local_now.replace(hour=1, minute=0)   # 8/18 01:00
    end_local = local_now.replace(hour=6, minute=0)     # 8/18 06:00
    sid = await make_schedule(test_user, start_at=start_local, end_at=end_local)

    async with async_session() as db:
        tz2, day_cfg = await get_store_day_config(db, test_store_id)
        today = get_work_date(tz2, day_cfg, now_utc)
        sched = await db.scalar(select(Schedule).where(Schedule.id == sid))
        # ── 시나리오 전제 검증 (여기가 틀리면 아래 결과는 의미가 없다) ──
        assert sched.operating_day == today - timedelta(days=1), (
            f"어제 영업일 라벨이어야 한다: label={sched.operating_day} today={today}"
        )
        assert sched.start_at.date() == sched.operating_day + timedelta(days=1), (
            "경계 이전 시작이므로 달력일은 영업일+1일이어야 한다(사용자 시나리오의 8/17 → 8/18 01:00)"
        )

    # 출근 상태로 만든다 (경계 넘기 전에 찍은 것).
    from app.services.attendance_lifecycle_service import ensure_attendance_for_schedule

    async with async_session() as db:
        sched = await db.scalar(select(Schedule).where(Schedule.id == sid))
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()
    async with async_session() as db:
        att = await db.scalar(select(Attendance).where(Attendance.schedule_id == sid))
        att.clock_in = now_utc - timedelta(hours=4)
        att.status = "working"
        await db.commit()

    # ① PIN 입력 — 화면이 오늘(8/18)을 보고 있어도 이 근무가 잡혀야 한다.
    identified = await async_client.post(
        IDENTIFY_URL, headers=device_auth_headers, json={"pin": test_user["clockin_pin"]},
    )
    assert identified.status_code == 200, identified.text
    body = identified.json()
    assert body["today_status"] == "working", (
        "today_status 가 없으면 앱이 Clock Out 을 띄울 근거가 없다"
    )
    ids = [i["schedule_id"] for i in body["today_attendances"]]
    assert str(sid) in ids, f"어제 영업일 근무가 목록에서 빠졌다: {ids}"

    # ② 퇴근 — 예정 종료 1시간 전이라 조기 퇴근 사유가 붙는다.
    co = await async_client.post(
        CLOCK_OUT_URL,
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "reason": "shift ended early",
        },
    )
    assert co.status_code == 200, co.text

    async with async_session() as db:
        closed = await db.scalar(select(Attendance).where(Attendance.schedule_id == sid))
        assert closed.clock_out is not None
        assert closed.status == "clocked_out"
