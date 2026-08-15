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

    # 한 액션 = 한 그룹, 그 안에 전이 항목마다 한 행 (status + clock_out).
    # before 는 어느 행에서도 비지 않는다.
    corrections = await _get_corrections(att_id)
    assert len(corrections) == 2
    assert {c.action for c in corrections} == {"auto_clock_out"}
    assert len({c.group_id for c in corrections}) == 1
    assert all(c.corrected_by is None for c in corrections)
    assert all(c.original_value for c in corrections)

    by_field = {c.field_name: c for c in corrections}
    assert by_field["status"].original_value == "working"
    assert by_field["status"].corrected_value == "clocked_out"
    assert by_field["clock_out"].original_value == "(none)"
    assert by_field["clock_out"].corrected_value == expected_out.isoformat()


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


# ── (D15) 겹친 근무는 자동 마감 대상에서 뺀다 ────────────────────────


async def _make_second_overdue_shift(
    test_user: dict, store_id: UUID, *, start: time, end: time, clock_in: time,
    clock_out: time | None = None,
) -> UUID:
    """같은 사람의 어제 두 번째 shift + attendance → attendance id.

    `clock_out` 을 주면 이미 닫힌 근무(= auto 대상 아님)가 된다.
    """
    yesterday = _yesterday_utc()
    async with async_session() as db:
        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=store_id,
            operating_day=yesterday,
            start_at=datetime.combine(yesterday, start),
            end_at=datetime.combine(yesterday, end),
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
            clock_in=datetime.combine(yesterday, clock_in, tzinfo=timezone.utc),
            clock_in_timezone="UTC",
            clock_out=(
                datetime.combine(yesterday, clock_out, tzinfo=timezone.utc)
                if clock_out
                else None
            ),
            clock_out_timezone="UTC" if clock_out else None,
            status="clocked_out" if clock_out else "working",
        )
        db.add(att)
        await db.commit()
        return att.id


async def test_auto_clock_out_skips_overlapping_open_shifts(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """겹쳐 열린 두 근무는 **둘 다** 자동 마감하지 않는다 (D15).

    자동으로 닫으면 두 shift 모두 "그럴듯하게 완결된 기록" 이 되어 **이중 지급이
    자동화**된다. 사람이 한쪽을 정정/취소해야 하고, 그때까지 `open_shift` 게이트가
    급여 확정을 막고 미퇴근 알림이 매니저를 계속 찌른다 — 의도한 압박이다.
    """
    # 09:00–12:00 (09:05 출근) + 11:00–15:00 (11:30 출근) → 11:30~12:00 이 겹친다.
    _sched_id, first_id = await _make_overdue_attendance(test_user, test_store_id)
    second_id = await _make_second_overdue_shift(
        test_user, test_store_id,
        start=time(11, 0), end=time(15, 0), clock_in=time(11, 30),
    )

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 0, "겹친 근무를 자동 마감했다 — 이중 지급 경로가 열린다"

    for att_id in (first_id, second_id):
        att = await _get_attendance(att_id)
        assert att.clock_out is None
        assert att.status == "working"
        assert "auto_clocked_out" not in (att.anomalies or [])


async def test_auto_clock_out_still_runs_next_to_a_closed_earlier_shift(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """앞 shift 를 제대로 닫고 온 사람의 열린 근무는 평소대로 자동 마감된다.

    겹침 skip 이 "하루 두 shift" 를 통째로 막아버리면 근무가 영영 안 닫히고
    `open_shift` 게이트가 매 기간 걸린다 — 오탐 쪽 비용도 크다는 뜻이라 경계를
    따로 못 박는다. (05:00–09:00 닫힘 + 09:00 시작 열림 = 맞닿기만 한다.)
    """
    await _make_second_overdue_shift(
        test_user, test_store_id,
        start=time(5, 0), end=time(9, 0), clock_in=time(5, 0), clock_out=time(9, 0),
    )
    _sched_id, open_id = await _make_overdue_attendance(test_user, test_store_id)

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 1

    att = await _get_attendance(open_id)
    assert att.clock_out is not None
    assert "auto_clocked_out" in (att.anomalies or [])


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


# ── (e) 종료시각 뒤에 지각 출근한 근무는 자동 마감하지 않는다 (AL-5) ─────────


async def test_late_clock_in_after_scheduled_end_is_not_auto_closed(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """clock_in 이 sched_end 보다 뒤면 자동 마감 대상에서 빠진다.

    shift 선택(④)으로 "종료시각이 지난 shift 에 지각 출근" 이 정상 경로가 되면서
    이 조합이 흔해졌다. 예전엔 clock_out=sched_end 로 찍혀 **clock_out < clock_in**
    인 음수 근무가 만들어졌다 — 실제로 재현된 사고다.
    """
    yesterday = _yesterday_utc()
    # 스케줄 09:00–12:00 인데 13:30 에 출근했다.
    late_in = datetime.combine(yesterday, time(13, 30), tzinfo=timezone.utc)
    _sched_id, att_id = await _make_overdue_attendance(
        test_user, test_store_id, status="late", clock_in=late_in,
    )

    async with async_session() as db:
        count = await _auto_clock_out_overdue(db)
    assert count == 0, "clock_in 보다 이른 cutoff 로 마감하면 안 된다"

    att = await _get_attendance(att_id)
    assert att.clock_out is None, "열린 채로 두고 사람이 정리하게 한다"
    assert att.status == "late"
    assert "auto_clocked_out" not in (att.anomalies or [])
    assert not await _get_corrections(att_id)
