"""Integration — 조기 clock-in override (예정보다 이른 출근을 사유와 함께 강행).

매니저/SV 가 현장에 없어도 "좀 더 일찍 와달라" 로 온 직원이 출근을 찍을 수 있어야
한다. 그래서 early threshold 이전 clock-in 은 차단이 아니라 **사유 요구**다.

라우터가 body 의 `reason` 을 서비스로 넘기지 않으면 사유를 적어도 계속 거부당한다 —
break 초과 사유에서 실제로 났던 배선 구멍이라, 여기서도 반드시 HTTP 요청으로 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.user_store import UserStore
from app.services.attendance_service import ANOMALY_EARLY_CLOCK_IN_OVERRIDE

pytestmark = pytest.mark.asyncio


async def _ensure_user_store(user_id: UUID, store_id: UUID) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id
            )
        )
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=False))
            await db.commit()


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    await _ensure_user_store(test_user["id"], test_store_id)


@pytest_asyncio.fixture
async def gm_user(test_users: dict) -> dict:
    return test_users["testgm"]


@pytest_asyncio.fixture
async def manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    gm_user: dict,
    test_store_id: UUID,
) -> dict:
    """device Authorization + X-Manage-Session — 매니저 대행 경로용."""
    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == gm_user["id"],
                UserStore.store_id == test_store_id,
            )
        )
        if existing is None:
            db.add(
                UserStore(
                    user_id=gm_user["id"], store_id=test_store_id, is_manager=True
                )
            )
        elif not existing.is_manager:
            existing.is_manager = True
        await db.commit()

    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm_user["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def _schedule_starting_in(
    make_schedule, test_user: dict, test_store_id: UUID, *, minutes: int
) -> None:
    """매장 현지시각 기준 `minutes` 분 뒤에 시작하는 스케줄을 만든다.

    고정 시각(09:00 등) 으로 잡으면 테스트 실행 시각에 따라 조기/지각 판정이 뒤집힌다.
    """
    from app.utils.timezone import get_store_day_config

    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, test_store_id)
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    # 시각(time)만 뽑아 넘기면 local_now + minutes 가 자정을 넘는 순간 랩어라운드해
    # "오늘 새벽" = 과거 스케줄이 되고 조기가 아니라 지각으로 판정된다.
    # 벽시계 datetime 을 통째로 넘겨 실행 시각과 무관하게 만든다.
    start_at = (local_now + timedelta(minutes=minutes)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    end_at = start_at + timedelta(minutes=240)
    await make_schedule(test_user, start_at=start_at, end_at=end_at)


async def _latest_attendance(user_id: UUID) -> Attendance:
    async with async_session() as db:
        return await db.scalar(
            select(Attendance)
            .where(Attendance.user_id == user_id)
            .order_by(Attendance.created_at.desc())
            .limit(1)
        )


async def test_early_clock_in_without_reason_is_rejected_with_code(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    """사유 없이 조기 clock-in → 400 + 구조화된 코드.

    앱이 사유 시트를 띄울지 판단해야 하므로 메시지 문자열이 아니라 code 로 구분된다.
    """
    await _ensure_user_store(test_user["id"], test_store_id)
    await _schedule_starting_in(
        make_schedule, test_user, test_store_id, minutes=120
    )

    res = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]},
    )

    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "early_clock_in_reason_required"
    # 분 단위 내림이라 119~120 사이로 떨어진다.
    assert 118 <= detail["minutes_early"] <= 120
    assert detail["schedule_id"]
    assert detail["scheduled_start"]
    assert "reason" in detail["message"].lower()

    # 거부됐으므로 출근 기록이 생기면 안 된다.
    att = await _latest_attendance(test_user["id"])
    assert att is None or att.clock_in is None


async def test_early_clock_in_with_reason_succeeds_and_is_labeled(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    """사유를 담아 재요청하면 통과 + override anomaly 라벨 + 사유가 이력에 남는다."""
    await _ensure_user_store(test_user["id"], test_store_id)
    await _schedule_starting_in(
        make_schedule, test_user, test_store_id, minutes=120
    )

    res = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "reason": "Asked to come in early",
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE in (body.get("anomalies") or [])
    assert body["clock_in"] is not None

    att = await _latest_attendance(test_user["id"])
    assert att.clock_in is not None
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE in (att.anomalies or [])
    # 직원이 스스로 강행한 건이므로 아직 미확인 — payroll 확정 전에 매니저가 봐야 한다.
    assert att.early_clock_in_confirmed_at is None

    # 사유는 Activity History(attendance_corrections) 에 남아야 매니저가 볼 수 있다.
    async with async_session() as db:
        reasons = (
            await db.execute(
                select(AttendanceCorrection.reason).where(
                    AttendanceCorrection.attendance_id == att.id
                )
            )
        ).scalars().all()
    assert "Asked to come in early" in [r for r in reasons if r]


async def test_clock_in_within_threshold_needs_no_reason(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    """threshold(기본 5분) 안쪽의 이른 출근은 기존대로 사유 없이 통과 + 라벨 없음."""
    await _ensure_user_store(test_user["id"], test_store_id)
    await _schedule_starting_in(make_schedule, test_user, test_store_id, minutes=3)

    res = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]},
    )

    assert res.status_code == 200, res.text
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE not in (res.json().get("anomalies") or [])


async def test_manager_override_is_labeled_but_needs_no_reason(
    async_client: AsyncClient,
    manage_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    make_schedule,
) -> None:
    """매니저 대행 조기 출근 — 사유 요구는 없지만 라벨은 붙는다.

    예정 밖 근무는 누가 찍었든 특이사항으로 보여야 한다. 다만 매니저가 그 자리에서
    승인한 행위라 사후 확인까지 요구하진 않는다(확인 처리는 Phase 2).
    """
    await _schedule_starting_in(make_schedule, test_user, test_store_id, minutes=120)

    res = await async_client.post(
        "/api/v1/attendance/manage/clock",
        headers=manage_headers,
        json={"user_id": str(test_user["id"]), "action": "clock_in"},
    )

    assert res.status_code == 200, res.text
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE in (res.json().get("anomalies") or [])
    # 확인은 이미 끝난 상태 — 매니저가 그 자리에서 승인했으므로 payroll 게이트에
    # 다시 걸리지 않는다.
    att = await _latest_attendance(test_user["id"])
    assert att.early_clock_in_confirmed_at is not None


async def test_blank_reason_is_not_accepted(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    """공백만 있는 사유는 사유가 없는 것으로 본다."""
    await _ensure_user_store(test_user["id"], test_store_id)
    await _schedule_starting_in(make_schedule, test_user, test_store_id, minutes=90)

    res = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "reason": "   ",
        },
    )

    assert res.status_code == 400, res.text
    assert res.json()["detail"]["code"] == "early_clock_in_reason_required"
