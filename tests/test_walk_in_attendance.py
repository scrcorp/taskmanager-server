"""Walk-in attendance tests — kiosk clock-in auto-creates a walk-in schedule.

Covers the server logic for the feat/walk-in-attendance feature:
  - clock-in walk_in branch (auto-create schedule when store allows + flag set)
  - rejection paths (setting off / flag off) keep the legacy "schedule required" behaviour
  - clock-out / break-start / break-end work on a walk-in attendance (D4, no regression)
  - DeviceMe response exposes walk_in_allowed (D10)
  - auto clock-out toggle (N2) — store with auto_clock_out_enabled=false is skipped

Isolation: HTTP-level tests reuse the device + store fixtures; store-level
settings are written directly to store_settings and torn down per test.
The registry rows are seeded at app startup (not migration), so each test that
relies on resolve_setting calls seed_settings_registry() first.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.settings import StoreSetting
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio

WALK_IN_KEY = "attendance.walk_in_allowed"
AUTO_OUT_KEY = "attendance.auto_clock_out_enabled"


# ── helpers ──────────────────────────────────────────────────────────


async def _seed_registry() -> None:
    from app.main import seed_settings_registry

    await seed_settings_registry()


async def _set_store_setting(store_id: UUID, key: str, value) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(StoreSetting).where(
                StoreSetting.store_id == store_id, StoreSetting.key == key
            )
        )
        if existing is not None:
            existing.value = value
        else:
            db.add(StoreSetting(store_id=store_id, key=key, value=value))
        await db.commit()


async def _clear_store_setting(store_id: UUID, key: str) -> None:
    async with async_session() as db:
        await db.execute(
            delete(StoreSetting).where(
                StoreSetting.store_id == store_id, StoreSetting.key == key
            )
        )
        await db.commit()


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


@pytest_asyncio.fixture(autouse=True)
async def _reset_walk_in_settings(test_store_id: UUID):
    """⚠️ 이 worktree DB 는 실행 중인 dev 서버와 공유된다. org-level 설정은 실제
    운영 구성(콘솔에서 켠 값)일 수 있으므로 **절대 삭제/수정하지 않는다.**
    대신 테스트 스토어의 store-level 을 명시적으로 세팅한다 — store override 가
    org-level 상속을 이기므로 org 값과 무관하게 결정적이다.
    baseline: walk_in=off, auto_clock_out=on. (개별 테스트가 필요시 store-level 재설정)"""
    await _seed_registry()
    await _set_store_setting(test_store_id, WALK_IN_KEY, False)
    await _set_store_setting(test_store_id, AUTO_OUT_KEY, True)
    yield
    for key in (WALK_IN_KEY, AUTO_OUT_KEY):
        await _clear_store_setting(test_store_id, key)


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    await _ensure_user_store(test_user["id"], test_store_id)


@pytest_asyncio.fixture
async def walk_in_enabled(test_store_id: UUID):
    """Enable walk_in_allowed for the test store; clean up afterwards."""
    await _seed_registry()
    await _set_store_setting(test_store_id, WALK_IN_KEY, True)
    yield
    await _clear_store_setting(test_store_id, WALK_IN_KEY)


# ── clock-in walk-in branch ──────────────────────────────────────────


async def test_walk_in_clock_in_creates_schedule_and_attendance(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    walk_in_enabled: None,
) -> None:
    """매장 walk_in 허용 + 스케줄 없음 + walk_in=true → 워크인 스케줄 자동 생성 후 출근."""
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "walk_in": True,
        },
    )
    assert resp.status_code == 200, resp.text

    async with async_session() as db:
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.user_id == test_user["id"],
                Schedule.store_id == test_store_id,
                Schedule.origin == "walk_in",
            )
        )
        assert sched is not None, "walk-in schedule was not created"
        assert sched.status == "confirmed"
        # 시각이 NULL 이 아니어야 함 (create-guard).
        assert sched.start_time is not None
        assert sched.end_time is not None
        # start = 실제 clock-in 시각(분 단위), end = start + 기본 근무시간(330분, mod 24h).
        start_min = sched.start_time.hour * 60 + sched.start_time.minute
        end_min = sched.end_time.hour * 60 + sched.end_time.minute
        assert end_min == (start_min + 330) % (24 * 60)

        att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == sched.id)
        )
        assert att is not None
        assert att.clock_in is not None
        # 워크인은 계획 시작 = 실제 출근이라 지각이 아니어야 함.
        assert att.status == "working"


async def test_walk_in_rejected_when_setting_off(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """walk_in=true 라도 store 설정이 꺼져 있으면 기존 거부 동작 유지."""
    await _seed_registry()
    # store-level 을 명시적으로 off (override) — org-level 상속과 무관하게 결정적.
    await _set_store_setting(test_store_id, WALK_IN_KEY, False)
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "walk_in": True,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "No scheduled shift" in resp.text

    async with async_session() as db:
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.user_id == test_user["id"],
                Schedule.origin == "walk_in",
            )
        )
        assert sched is None


async def test_walk_in_flag_false_rejected_even_when_allowed(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    walk_in_enabled: None,
) -> None:
    """store 가 walk_in 허용이어도 요청 walk_in=false 면 자동 생성하지 않고 거부."""
    resp = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "walk_in": False,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "No scheduled shift" in resp.text


async def test_walk_in_break_and_clock_out_work(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    walk_in_enabled: None,
) -> None:
    """워크인 출근 후 break-start / break-end / clock-out 전부 정상 (D4 회귀 방지)."""
    base = {"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]}

    clock_in = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={**base, "walk_in": True},
    )
    assert clock_in.status_code == 200, clock_in.text

    bs = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={**base, "break_type": "paid_10min"},
    )
    assert bs.status_code == 200, bs.text

    # Backdate the open break so break-end clears the 10-minute-minimum policy
    # (timing policy is generic, not walk-in specific — we only assert the
    # action succeeds on a walk-in attendance).
    from app.models.attendance_break import AttendanceBreak

    async with async_session() as db:
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.user_id == test_user["id"], Schedule.origin == "walk_in"
            )
        )
        att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == sched.id)
        )
        open_break = await db.scalar(
            select(AttendanceBreak).where(
                AttendanceBreak.attendance_id == att.id,
                AttendanceBreak.ended_at.is_(None),
            )
        )
        open_break.started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        await db.commit()

    be = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json=base,
    )
    assert be.status_code == 200, be.text

    co = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={**base, "reason": "walk-in done"},
    )
    assert co.status_code == 200, co.text

    async with async_session() as db:
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.user_id == test_user["id"], Schedule.origin == "walk_in"
            )
        )
        att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == sched.id)
        )
        assert att.clock_out is not None
        assert att.status == "clocked_out"


async def test_walk_in_reclockin_after_clockout_creates_new_schedule(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    walk_in_enabled: None,
) -> None:
    """워크인 출근 → 퇴근 → 다시 출근 시 새 워크인 스케줄이 생성된다(하루 여러 shift).

    첫 워크인이 clocked_out 이면 열린 후보가 없으므로, walk_in 요청 시 두 번째
    워크인 스케줄을 만들어 재출근을 허용해야 한다.
    """
    base = {"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]}

    ci1 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={**base, "walk_in": True},
    )
    assert ci1.status_code == 200, ci1.text

    # 즉시 퇴근은 계획 end(=clock-in+기본시간) 이전이라 early → reason 필요.
    co = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={**base, "reason": "first shift done"},
    )
    assert co.status_code == 200, co.text

    # 첫(끝난) 스케줄 id 를 확보 — 클라(키오스크)가 재출근 시 selectedScheduleId 로
    # 이 clocked_out id 를 실어보내는 버그 상황을 재현. 서버는 워크인 생성 경로에서
    # 이 stale schedule_id 를 무시하고 새 스케줄을 써야 한다.
    async with async_session() as db:
        first_sched = await db.scalar(
            select(Schedule).where(
                Schedule.user_id == test_user["id"],
                Schedule.store_id == test_store_id,
                Schedule.origin == "walk_in",
            )
        )
        stale_schedule_id = str(first_sched.id)

    ci2 = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={**base, "walk_in": True, "schedule_id": stale_schedule_id},
    )
    assert ci2.status_code == 200, ci2.text

    async with async_session() as db:
        scheds = (
            await db.execute(
                select(Schedule).where(
                    Schedule.user_id == test_user["id"],
                    Schedule.store_id == test_store_id,
                    Schedule.origin == "walk_in",
                )
            )
        ).scalars().all()
        assert len(scheds) == 2, f"expected 2 walk-in schedules, got {len(scheds)}"
        # 두 번째는 열려 있고(clock_in 있음), 첫 번째는 clocked_out.
        atts = (
            await db.execute(
                select(Attendance).where(
                    Attendance.schedule_id.in_([s.id for s in scheds])
                )
            )
        ).scalars().all()
        statuses = sorted(a.status for a in atts)
        assert statuses == ["clocked_out", "working"], statuses


# ── DeviceMe walk_in_allowed (D10) ───────────────────────────────────


async def test_device_me_exposes_walk_in_allowed(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
) -> None:
    """DeviceMe 응답에 매장 resolve 된 walk_in_allowed 가 노출된다."""
    await _seed_registry()

    await _set_store_setting(test_store_id, WALK_IN_KEY, True)
    try:
        resp = await async_client.get(
            "/api/v1/attendance/me", headers=device_auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["walk_in_allowed"] is True
    finally:
        # store-level 을 명시적으로 off (override) — org-level 상속과 무관하게 false 보장.
        await _set_store_setting(test_store_id, WALK_IN_KEY, False)

    # store override=false 면 DeviceMe 도 false
    resp2 = await async_client.get(
        "/api/v1/attendance/me", headers=device_auth_headers
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["walk_in_allowed"] is False


# ── auto clock-out toggle (N2) ───────────────────────────────────────


async def _make_overdue_walk_in_attendance(
    test_user: dict, store_id: UUID
) -> tuple[UUID, UUID]:
    """어제 날짜 walk-in 스케줄 + 미퇴근(open) attendance 를 만든다.

    end_time 이 한참 지났으므로 auto clock-out 대상이 된다 (UTC store).
    """
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    async with async_session() as db:
        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=store_id,
            work_date=yesterday,
            start_time=time(9, 0),
            end_time=time(12, 0),
            status="confirmed",
            origin="walk_in",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=test_user["organization_id"],
            store_id=store_id,
            user_id=test_user["id"],
            schedule_id=sched.id,
            work_date=yesterday,
            clock_in=datetime.combine(yesterday, time(9, 5), tzinfo=timezone.utc),
            clock_in_timezone="UTC",
            status="working",
        )
        db.add(att)
        await db.commit()
        return sched.id, att.id


async def test_auto_clock_out_skipped_when_disabled(
    test_user: dict,
    test_store_id: UUID,
    _clean_state: None,
) -> None:
    """auto_clock_out_enabled=false 매장은 자동 퇴근에서 skip 된다."""
    from app.services.attendance_cron_service import _auto_clock_out_overdue

    await _seed_registry()
    await _set_store_setting(test_store_id, AUTO_OUT_KEY, False)
    try:
        _sched_id, att_id = await _make_overdue_walk_in_attendance(
            test_user, test_store_id
        )
        async with async_session() as db:
            await _auto_clock_out_overdue(db)
        async with async_session() as db:
            att = await db.scalar(select(Attendance).where(Attendance.id == att_id))
            assert att.clock_out is None, "disabled store must not be auto-clocked-out"
            assert att.status == "working"
    finally:
        await _clear_store_setting(test_store_id, AUTO_OUT_KEY)


async def test_auto_clock_out_runs_when_enabled(
    test_user: dict,
    test_store_id: UUID,
    _clean_state: None,
) -> None:
    """auto_clock_out_enabled=true(기본) 매장은 자동 퇴근이 동작한다."""
    from app.services.attendance_cron_service import _auto_clock_out_overdue

    await _seed_registry()
    await _set_store_setting(test_store_id, AUTO_OUT_KEY, True)
    try:
        _sched_id, att_id = await _make_overdue_walk_in_attendance(
            test_user, test_store_id
        )
        async with async_session() as db:
            await _auto_clock_out_overdue(db)
        async with async_session() as db:
            att = await db.scalar(select(Attendance).where(Attendance.id == att_id))
            assert att.clock_out is not None
            assert att.status == "clocked_out"
            assert "auto_clocked_out" in (att.anomalies or [])
    finally:
        await _clear_store_setting(test_store_id, AUTO_OUT_KEY)
