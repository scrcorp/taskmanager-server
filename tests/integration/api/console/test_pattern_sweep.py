"""고정 근무 — sweep (계약 §7 test_pattern_sweep).

- overridden 행은 건너뛴다(값 보존)
- sweep 은 overridden 플래그를 **절대 켜지 않는다**
- 두 번 sweep 해도 결과 동일(멱등), 모두 적용 상태 유지
- deleted 행 제외
- 요일이 빠진 날의 자동 행은 delete_entry 로 정리, 새 요일은 materialize 로 채움
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.database import async_session
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule import ScheduleUpdate
from app.services.fixed_schedule import patterns as svc
from app.services.fixed_schedule.materialize import sweep_group
from tests.integration.api.console.test_pattern_groups import (  # noqa: F401
    ALL_DAYS,
    TODAY,
    admin_session,
    block,
    create,
    schedules_of,
    staff_assigned,
)

pytestmark = pytest.mark.asyncio

EDIT_DAY = TODAY + timedelta(days=3)
DELETE_DAY = TODAY + timedelta(days=5)


def _iso(d, hour: int) -> str:
    return datetime.combine(d, datetime.min.time()).replace(hour=hour).strftime("%Y-%m-%dT%H:%M")


async def _shift_pattern_times(group_id: str, start: time, end: time) -> None:
    """패턴 원장 값을 직접 바꾼다 — sweep 자체만 보기 위해 update_group 을 거치지 않는다."""
    async with async_session() as db:
        await db.execute(
            update(StaffWorkPattern).where(StaffWorkPattern.group_id == UUID(group_id))
            .values(start_time=start, end_time=end)
        )
        await db.commit()


@pytest.fixture
async def swept_setup(staff_assigned):
    """오늘부터 매일 09–17 (15행) + EDIT_DAY 는 사람이 고침(overridden) + DELETE_DAY 는 삭제."""
    g = await create(staff_assigned, start_date=TODAY, blocks=[block("09:00", "17:00", ALL_DAYS)])
    pid = UUID(g.blocks[0].id)
    async with admin_session(staff_assigned) as (db, admin):
        await svc.materialize_occurrence(
            db, organization_id=staff_assigned["organization_id"], actor=admin,
            pattern_id=pid, occurrence_date=EDIT_DAY, action="edit",
            patch=ScheduleUpdate(start_at=_iso(EDIT_DAY, 12), end_at=_iso(EDIT_DAY, 20), force=True),
        )
        await svc.materialize_occurrence(
            db, organization_id=staff_assigned["organization_id"], actor=admin,
            pattern_id=pid, occurrence_date=DELETE_DAY, action="delete", patch=None,
        )
    return g


async def _sweep(group_id: str):
    async with async_session() as db:
        return await sweep_group(db, group_id=UUID(group_id))


class TestSweep:
    async def test_updates_untouched_skips_overridden_excludes_deleted(self, staff_assigned, swept_setup):
        await _shift_pattern_times(swept_setup.group_id, time(10, 0), time(18, 0))
        updated, skipped = await _sweep(swept_setup.group_id)
        assert (updated, skipped) == (13, 1)  # 15 - edited - deleted = 13 갱신, overridden 1 건너뜀

        rows = await schedules_of(staff_assigned)
        by_day = {r.operating_day: r for r in rows}
        for d, r in by_day.items():
            if d == EDIT_DAY:
                assert r.pattern_overridden is True and r.start_at.hour == 12  # 보존
            elif d == DELETE_DAY:
                assert r.status == "deleted" and r.start_at.hour == 9  # 제외(건드리지 않음)
            else:
                assert r.status == "confirmed"
                assert r.start_at.hour == 10 and r.end_at.hour == 18
                assert r.pattern_overridden is False  # 절대 켜지 않는다

    async def test_sweep_twice_is_idempotent(self, staff_assigned, swept_setup):
        await _shift_pattern_times(swept_setup.group_id, time(10, 0), time(18, 0))
        first = await _sweep(swept_setup.group_id)
        second = await _sweep(swept_setup.group_id)
        assert first == (13, 1)
        assert second == (0, 1)
        rows = await schedules_of(staff_assigned, include_deleted=False)
        untouched = [r for r in rows if r.operating_day != EDIT_DAY]
        assert all(r.start_at.hour == 10 and not r.pattern_overridden for r in untouched)

    async def test_second_pattern_change_is_applied(self, staff_assigned, swept_setup):
        """A 변경→sweep→B 변경→sweep: sweep 이 overridden 을 켜지 않으므로 두 번째 변경도 전부 먹는다."""
        await _shift_pattern_times(swept_setup.group_id, time(10, 0), time(18, 0))
        assert await _sweep(swept_setup.group_id) == (13, 1)
        await _shift_pattern_times(swept_setup.group_id, time(11, 0), time(19, 0))
        assert await _sweep(swept_setup.group_id) == (13, 1)
        rows = await schedules_of(staff_assigned, include_deleted=False)
        for r in rows:
            if r.operating_day == EDIT_DAY:
                assert r.pattern_overridden is True and r.start_at.hour == 12  # 사람 수정 보존
            else:
                assert r.start_at.hour == 11 and r.end_at.hour == 19 and r.pattern_overridden is False

    async def test_sweep_without_change_is_noop(self, staff_assigned, swept_setup):
        assert await _sweep(swept_setup.group_id) == (0, 1)

    async def test_sweep_keeps_audit_reason(self, staff_assigned, swept_setup):
        from app.models.schedule import ScheduleAuditLog
        await _shift_pattern_times(swept_setup.group_id, time(10, 0), time(18, 0))
        await _sweep(swept_setup.group_id)
        rows = await schedules_of(staff_assigned, include_deleted=False)
        target = next(r for r in rows if r.operating_day == TODAY + timedelta(days=1))
        async with async_session() as db:
            logs = (await db.execute(select(ScheduleAuditLog).where(
                ScheduleAuditLog.schedule_id == target.id, ScheduleAuditLog.event_type == "modified",
            ))).scalars().all()
        assert logs and logs[-1].reason == "pattern_swept"

    async def test_removed_weekday_rows_are_deleted_by_sweep(self, staff_assigned, swept_setup):
        """byday 에서 빠진 요일의 자동 행은 delete_entry 로 정리된다(overridden 은 남는다)."""
        from app.services.fixed_schedule.expand import dow_sun0
        keep = dow_sun0(EDIT_DAY)
        async with async_session() as db:
            await db.execute(
                update(StaffWorkPattern).where(StaffWorkPattern.group_id == UUID(swept_setup.group_id))
                .values(byday=[keep])
            )
            await db.commit()
        await _sweep(swept_setup.group_id)
        rows = await schedules_of(staff_assigned, include_deleted=False)
        assert rows and all(dow_sun0(r.operating_day) == keep for r in rows)
        assert any(r.operating_day == EDIT_DAY and r.pattern_overridden for r in rows)
