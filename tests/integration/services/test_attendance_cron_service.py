"""Integration tests — attendance_cron_service (AL-4 / P0-6).

자동 clock-out cron 이 실제 clock_out 을 절대 덮어쓰지 않는지 검증:
  - (a) 진짜 미퇴근(open) attendance 는 기존과 동일하게 자동 종료
        (clock_out=sched_end, break 종료, anomaly, correction corrected_by NULL)
  - (b) clock_out 이 이미 있는데 status 만 stale(open) 인 row 는
        clock_out 을 건드리지 않고 status 만 정합화(reconcile)
  - (c) fetch 와 write 사이에 직원이 실제 clock-out 한 race →
        write-time 원자 가드(WHERE clock_out IS NULL)가 덮어쓰기를 차단
  - reconcile 은 이미 닫힌 attendance 의 break 를 건드리지 않음

Isolation: worktree DB + `_clean_state` (attendance/schedule purge per test).
설정 registry 미시드 상태에서도 기본값(auto ON / after 30min)으로 동작하므로
별도 설정 시드는 하지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.attendance_break import BREAK_TYPE_UNPAID_MEAL, AttendanceBreak
from app.models.schedule import Schedule
from app.services.attendance_cron_service import (
    _auto_clock_out_overdue,
    _persist_late_and_no_show,
    _reconcile_closed_attendance_status,
)


pytestmark = pytest.mark.asyncio


def _yesterday_utc() -> date:
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


async def _make_overdue_attendance(
    test_user: dict,
    store_id: UUID,
    *,
    status: str = "working",
    clock_in: datetime | None = "default",  # sentinel: 어제 09:05 UTC
    clock_out: datetime | None = None,
    total_work_minutes: int | None = None,
) -> tuple[UUID, UUID]:
    """어제 09:00–12:00 스케줄 + attendance 생성 (UTC store 기준 auto 대상)."""
    yesterday = _yesterday_utc()
    if clock_in == "default":
        clock_in = datetime.combine(yesterday, time(9, 5), tzinfo=timezone.utc)
    async with async_session() as db:
        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=store_id,
            operating_day=yesterday,
            start_at=datetime.combine(yesterday, time(9, 0)),
            end_at=datetime.combine(yesterday, time(12, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=test_user["organization_id"],
            store_id=store_id,
            user_id=test_user["id"],
            schedule_id=sched.id,
            work_date=yesterday,
            clock_in=clock_in,
            clock_in_timezone="UTC",
            clock_out=clock_out,
            clock_out_timezone="UTC" if clock_out else None,
            status=status,
            total_work_minutes=total_work_minutes,
        )
        db.add(att)
        await db.commit()
        return sched.id, att.id


async def _get_attendance(att_id: UUID) -> Attendance:
    async with async_session() as db:
        return (
            await db.execute(select(Attendance).where(Attendance.id == att_id))
        ).scalar_one()


async def _get_corrections(att_id: UUID) -> list[AttendanceCorrection]:
    async with async_session() as db:
        return list(
            (
                await db.execute(
                    select(AttendanceCorrection).where(
                        AttendanceCorrection.attendance_id == att_id
                    )
                )
            ).scalars().all()
        )


# ── (a) 진짜 open shift 는 기존과 동일하게 자동 종료 ─────────────────


async def test_open_overdue_shift_auto_closed_as_before(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """clock_out 없는 working attendance → sched_end 로 자동 퇴근 + correction."""
    _sched_id, att_id = await _make_overdue_attendance(test_user, test_store_id)

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 1

    att = await _get_attendance(att_id)
    expected_out = datetime.combine(_yesterday_utc(), time(12, 0), tzinfo=timezone.utc)
    assert att.clock_out == expected_out
    assert att.status == "clocked_out"
    assert "auto_clocked_out" in (att.anomalies or [])
    assert att.total_work_minutes == 175  # 09:05 → 12:00

    corrections = await _get_corrections(att_id)
    assert len(corrections) == 1
    corr = corrections[0]
    assert corr.field_name == "auto_clock_out"
    assert corr.corrected_by is None
    assert corr.corrected_value == expected_out.isoformat()


async def test_open_overdue_on_break_closes_break_at_cutoff(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """on_break + 진행중 break → break 를 sched_end 시점으로 종료하고 합산."""
    _sched_id, att_id = await _make_overdue_attendance(
        test_user, test_store_id, status="on_break"
    )
    break_start = datetime.combine(_yesterday_utc(), time(11, 0), tzinfo=timezone.utc)
    async with async_session() as db:
        db.add(
            AttendanceBreak(
                attendance_id=att_id,
                started_at=break_start,
                break_type=BREAK_TYPE_UNPAID_MEAL,
            )
        )
        await db.commit()

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 1

    att = await _get_attendance(att_id)
    expected_out = datetime.combine(_yesterday_utc(), time(12, 0), tzinfo=timezone.utc)
    assert att.clock_out == expected_out
    assert att.status == "clocked_out"
    assert att.break_end == expected_out
    assert att.total_break_minutes == 60  # 11:00 → 12:00

    async with async_session() as db:
        br = (
            await db.execute(
                select(AttendanceBreak).where(AttendanceBreak.attendance_id == att_id)
            )
        ).scalar_one()
    assert br.ended_at == expected_out
    assert br.duration_minutes == 60


# ── (b) 이미 닫힌 row (stale status) — clock_out 보존 + status 정합화 ─


async def test_stale_status_with_real_clock_out_is_not_overwritten(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """clock_out=11:47 인데 status='working' stale → auto clock-out 은 skip,
    reconcile 이 status 만 clocked_out 으로 고친다. clock_out/분은 그대로."""
    real_out = datetime.combine(_yesterday_utc(), time(11, 47), tzinfo=timezone.utc)
    _sched_id, att_id = await _make_overdue_attendance(
        test_user, test_store_id,
        status="working", clock_out=real_out, total_work_minutes=162,
    )

    # 1) auto clock-out 은 이 row 를 절대 건드리지 않는다 (쿼리 가드)
    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 0
    att = await _get_attendance(att_id)
    assert att.clock_out == real_out
    assert att.total_work_minutes == 162
    assert "auto_clocked_out" not in (att.anomalies or [])

    # 2) reconcile 이 status 만 정합화한다
    async with async_session() as db:
        reconciled = await _reconcile_closed_attendance_status(db)
    assert reconciled == 1
    att = await _get_attendance(att_id)
    assert att.clock_out == real_out  # 실제 퇴근시각 보존 — 급여 기준
    assert att.status == "clocked_out"
    assert att.total_work_minutes == 162
    assert "auto_clocked_out" not in (att.anomalies or [])
    assert await _get_corrections(att_id) == []


async def test_reconcile_does_not_touch_breaks_of_closed_attendance(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """이미 닫힌(clock_out 有) attendance 의 open break 는 cron 이 건드리지 않는다."""
    real_out = datetime.combine(_yesterday_utc(), time(11, 30), tzinfo=timezone.utc)
    _sched_id, att_id = await _make_overdue_attendance(
        test_user, test_store_id, status="on_break", clock_out=real_out,
    )
    break_start = datetime.combine(_yesterday_utc(), time(11, 0), tzinfo=timezone.utc)
    async with async_session() as db:
        db.add(
            AttendanceBreak(
                attendance_id=att_id,
                started_at=break_start,
                break_type=BREAK_TYPE_UNPAID_MEAL,
            )
        )
        await db.commit()

    async with async_session() as db:
        await _reconcile_closed_attendance_status(db)
    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 0

    att = await _get_attendance(att_id)
    assert att.status == "clocked_out"
    assert att.clock_out == real_out
    assert att.break_end is None
    async with async_session() as db:
        br = (
            await db.execute(
                select(AttendanceBreak).where(AttendanceBreak.attendance_id == att_id)
            )
        ).scalar_one()
    assert br.ended_at is None  # 닫힌 attendance 의 break 는 그대로
    assert br.duration_minutes is None


async def test_reconcile_leaves_genuinely_open_rows_alone(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """clock_out 없는 진짜 open row 는 reconcile 대상이 아니다 (auto 경로 유지)."""
    _sched_id, att_id = await _make_overdue_attendance(test_user, test_store_id)

    async with async_session() as db:
        reconciled = await _reconcile_closed_attendance_status(db)
    assert reconciled == 0
    att = await _get_attendance(att_id)
    assert att.status == "working"
    assert att.clock_out is None


async def test_no_show_promotion_skips_row_with_real_clock_out(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """clock_in 이 없어도 clock_out 이 있으면(실제 근무 기록) no_show 로 강등 금지."""
    real_out = datetime.combine(_yesterday_utc(), time(11, 47), tzinfo=timezone.utc)
    _sched_id, att_id = await _make_overdue_attendance(
        test_user, test_store_id, status="late", clock_in=None, clock_out=real_out,
    )

    async with async_session() as db:
        await _persist_late_and_no_show(db)

    att = await _get_attendance(att_id)
    assert att.status == "late"  # no_show 로 클로버링되지 않음
    assert att.clock_out == real_out
    assert "no_show" not in (att.anomalies or [])


# ── (c) fetch↔write race — write-time 원자 가드 ──────────────────────


async def test_write_time_guard_blocks_concurrent_real_clock_out(
    test_user: dict, test_store_id: UUID, _clean_state: None, monkeypatch,
) -> None:
    """cron 이 row 를 open 으로 fetch 한 뒤, write 전에 직원이 실제 clock-out →
    조건부 UPDATE(WHERE clock_out IS NULL) 가 rowcount=0 으로 덮어쓰기 차단."""
    from app.services import attendance_cron_service as cron

    _sched_id, att_id = await _make_overdue_attendance(test_user, test_store_id)
    real_out = datetime.combine(_yesterday_utc(), time(11, 52), tzinfo=timezone.utc)

    real_resolver = cron.resolve_setting
    fired = False

    async def racing_resolver(db, *, key, organization_id, store_id):
        # cron 의 fetch 이후 첫 per-row await 시점에 "직원의 실제 clock-out" 을
        # 별도 세션으로 커밋해 race 를 재현한다.
        nonlocal fired
        if not fired:
            fired = True
            async with async_session() as db2:
                await db2.execute(
                    update(Attendance)
                    .where(Attendance.id == att_id)
                    .values(
                        clock_out=real_out,
                        clock_out_timezone="UTC",
                        status="clocked_out",
                        total_work_minutes=167,
                    )
                )
                await db2.commit()
        return await real_resolver(
            db, key=key, organization_id=organization_id, store_id=store_id
        )

    monkeypatch.setattr(cron, "resolve_setting", racing_resolver)

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)

    assert fired, "race hook must have run (row was fetched as open)"
    assert count == 0  # 이 row 는 auto clock-out 처리되지 않아야 함

    att = await _get_attendance(att_id)
    assert att.clock_out == real_out  # 실제 퇴근시각 보존
    assert att.status == "clocked_out"
    assert att.total_work_minutes == 167
    assert "auto_clocked_out" not in (att.anomalies or [])
    assert await _get_corrections(att_id) == []
