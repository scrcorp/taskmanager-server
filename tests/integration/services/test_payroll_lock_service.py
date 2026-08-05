"""Integration tests — payroll_lock_service (L3 lock 판정 헬퍼) + cron skip.

검증 대상:
    - ensure_not_locked: confirmed 기간 안 날짜만 409 (PayPeriodLockedError),
      open 기간/기간 없음/타 매장/None 인자는 통과. 경계: end_date 는 잠김,
      end_date+1 은 통과 (저장된 [start, end] 포함 범위 그대로).
    - is_locked_cached: 같은 (store, date) 재조회 없이 캐시 사용, bool 반환.
    - attendance cron: locked 날짜 row 는 auto clock-out / late·no_show 승격 모두
      건드리지 않는다. 기간이 open 으로 돌아오면 정상 처리.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.payroll import PayPeriod
from app.models.schedule import Schedule
from app.services.attendance_cron_service import (
    _auto_clock_out_overdue,
    _persist_late_and_no_show,
)
from app.services.payroll_lock_service import (
    PAY_PERIOD_LOCKED_MESSAGE,
    PayPeriodLockedError,
    ensure_not_locked,
    is_locked_cached,
)
from app.services.payroll_period_service import period_bounds_for

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 픽스처 — throwaway store (공용 시드 오염 방지)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lock_env(seed_organization: dict, test_users: dict) -> AsyncIterator[dict]:
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__lock_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        env = {
            "org_id": org_id,
            "store_id": store.id,
            "staff_id": test_users["teststaff"]["id"],
        }

    yield env

    async with async_session() as db:
        await db.execute(delete(Attendance).where(Attendance.store_id == env["store_id"]))
        await db.execute(delete(Schedule).where(Schedule.store_id == env["store_id"]))
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == env["store_id"]))
        await db.execute(delete(Store).where(Store.id == env["store_id"]))
        await db.commit()


async def _insert_period(env: dict, start: date, end: date, status: str) -> UUID:
    async with async_session() as db:
        period = PayPeriod(
            organization_id=env["org_id"],
            store_id=env["store_id"],
            start_date=start,
            end_date=end,
            status=status,
        )
        db.add(period)
        await db.commit()
        return period.id


# ---------------------------------------------------------------------------
# ensure_not_locked
# ---------------------------------------------------------------------------


async def test_no_period_passes(db, lock_env):
    """기간 자체가 없으면 어떤 날짜든 통과."""
    await ensure_not_locked(db, store_id=lock_env["store_id"], work_date=date(2026, 7, 20))


async def test_open_period_passes(db, lock_env):
    """open 기간 안 날짜는 통과 — confirmed 만 잠근다."""
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "open")
    await ensure_not_locked(db, store_id=lock_env["store_id"], work_date=date(2026, 7, 20))


async def test_confirmed_period_raises_409(db, lock_env):
    """confirmed 기간 안 날짜는 PayPeriodLockedError(409) + 고정 메시지."""
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "confirmed")
    with pytest.raises(PayPeriodLockedError) as exc_info:
        await ensure_not_locked(
            db, store_id=lock_env["store_id"], work_date=date(2026, 7, 20)
        )
    assert exc_info.value.status_code == 409
    assert PAY_PERIOD_LOCKED_MESSAGE in exc_info.value.detail
    assert "2026-07-20" in exc_info.value.detail


async def test_boundary_end_date_locked_next_day_open(db, lock_env):
    """경계: 기간 end_date 는 잠김, end_date+1 은 통과."""
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "confirmed")
    with pytest.raises(PayPeriodLockedError):
        await ensure_not_locked(
            db, store_id=lock_env["store_id"], work_date=date(2026, 7, 31)
        )
    # 다음 날 (다음 기간 시작일, 기간 없음) — 통과
    await ensure_not_locked(db, store_id=lock_env["store_id"], work_date=date(2026, 8, 1))
    # 기간 시작 전날도 통과
    await ensure_not_locked(db, store_id=lock_env["store_id"], work_date=date(2026, 7, 15))


async def test_other_store_not_locked(db, lock_env, test_store_id):
    """다른 매장의 confirmed 기간은 이 매장 판정에 영향 없음."""
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "confirmed")
    await ensure_not_locked(db, store_id=test_store_id, work_date=date(2026, 7, 20))


async def test_none_store_or_date_passes(db, lock_env):
    """store/date 유실 row 는 판정 불가 → 통과 (SET NULL 관례)."""
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "confirmed")
    await ensure_not_locked(db, store_id=None, work_date=date(2026, 7, 20))
    await ensure_not_locked(db, store_id=lock_env["store_id"], work_date=None)


async def test_is_locked_cached_uses_cache(db, lock_env):
    """같은 (store, date) 는 캐시 재사용 — 기간이 뒤에 확정돼도 캐시값 유지."""
    cache: dict = {}
    locked = await is_locked_cached(
        db, cache, store_id=lock_env["store_id"], work_date=date(2026, 7, 20)
    )
    assert locked is False
    await _insert_period(lock_env, date(2026, 7, 16), date(2026, 7, 31), "confirmed")
    # 캐시 hit — DB 재조회 없이 False 유지 (한 루프 안에서의 일관성)
    assert await is_locked_cached(
        db, cache, store_id=lock_env["store_id"], work_date=date(2026, 7, 20)
    ) is False
    # 새 캐시로는 True
    assert await is_locked_cached(
        db, {}, store_id=lock_env["store_id"], work_date=date(2026, 7, 20)
    ) is True


# ---------------------------------------------------------------------------
# Cron skip — locked 날짜 row 는 자동퇴근/승격 대상에서 제외
# ---------------------------------------------------------------------------


async def _make_overdue_attendance(env: dict, *, status: str, with_clock_in: bool) -> UUID:
    """오늘(UTC) 날짜에 이미 종료됐어야 할 schedule + attendance 생성."""
    now = datetime.now(timezone.utc)
    today = now.date()
    start_naive = (now - timedelta(hours=6)).replace(tzinfo=None, second=0, microsecond=0)
    end_naive = (now - timedelta(hours=2)).replace(tzinfo=None, second=0, microsecond=0)
    async with async_session() as db:
        sched = Schedule(
            organization_id=env["org_id"],
            user_id=env["staff_id"],
            store_id=env["store_id"],
            operating_day=today,
            start_at=start_naive,
            end_at=end_naive,
            status="confirmed",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=env["org_id"],
            store_id=env["store_id"],
            user_id=env["staff_id"],
            schedule_id=sched.id,
            work_date=today,
            clock_in=(now - timedelta(hours=6)) if with_clock_in else None,
            clock_in_timezone="UTC" if with_clock_in else None,
            status=status,
        )
        db.add(att)
        await db.commit()
        return att.id


async def test_cron_auto_clock_out_skips_locked_date(db, lock_env):
    """오늘이 confirmed 기간 안이면 auto clock-out cron 이 row 를 건너뛴다."""
    today = datetime.now(timezone.utc).date()
    start, end = period_bounds_for(today)
    period_id = await _insert_period(lock_env, start, end, "confirmed")
    att_id = await _make_overdue_attendance(lock_env, status="working", with_clock_in=True)

    async with async_session() as run_db:
        await _auto_clock_out_overdue(run_db)

    async with async_session() as check_db:
        att = (await check_db.execute(
            select(Attendance).where(Attendance.id == att_id)
        )).scalar_one()
        assert att.clock_out is None, "locked 날짜 row 가 자동퇴근 처리되면 안 됨"
        assert att.status == "working"

    # 기간을 open 으로 되돌리면 정상 처리
    async with async_session() as unlock_db:
        period = (await unlock_db.execute(
            select(PayPeriod).where(PayPeriod.id == period_id)
        )).scalar_one()
        period.status = "open"
        await unlock_db.commit()

    async with async_session() as run_db:
        await _auto_clock_out_overdue(run_db)

    async with async_session() as check_db:
        att = (await check_db.execute(
            select(Attendance).where(Attendance.id == att_id)
        )).scalar_one()
        assert att.clock_out is not None
        assert att.status == "clocked_out"
        assert "auto_clocked_out" in (att.anomalies or [])


async def test_cron_late_no_show_promotion_skips_locked_date(db, lock_env):
    """locked 날짜의 upcoming row 는 late/no_show 승격도 하지 않는다."""
    today = datetime.now(timezone.utc).date()
    start, end = period_bounds_for(today)
    await _insert_period(lock_env, start, end, "confirmed")
    # 미출근 + 스케줄 종료 지남 → 평소라면 no_show 로 승격될 row
    att_id = await _make_overdue_attendance(lock_env, status="upcoming", with_clock_in=False)

    async with async_session() as run_db:
        await _persist_late_and_no_show(run_db)

    async with async_session() as check_db:
        att = (await check_db.execute(
            select(Attendance).where(Attendance.id == att_id)
        )).scalar_one()
        assert att.status == "upcoming", "locked 날짜 row 는 승격되면 안 됨"
