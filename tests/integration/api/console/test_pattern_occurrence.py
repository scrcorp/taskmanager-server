"""고정 근무 — occurrence 실체화 / revert / duplicate (계약 §7 test_pattern_occurrence).

- edit 실체화: 실 행 + 도장 + overridden=True, 슬롯 유니크
- 같은 슬롯 재-edit 는 같은 행에 적용(행 수 1)
- delete 실체화: status=deleted + overridden=True, 슬롯 점유 → 재실체화 no-op
- revert: overridden 만 / deleted 거부 / 미손댐 거부 (409 PATTERN_REVERT_NOT_OVERRIDDEN)
- duplicate(일반 create_entry) 는 stamp 없음
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.schedule import Schedule
from app.schemas.schedule import ScheduleCreate, ScheduleUpdate
from app.services.fixed_schedule import patterns as svc
from app.services.fixed_schedule.materialize import materialize_window
from app.services.schedule_service import schedule_service
from app.utils.exceptions import AppError
from tests.integration.api.console.test_pattern_groups import (  # noqa: F401
    ALL_DAYS,
    FAR,
    TODAY,
    admin_session,
    block,
    create,
    schedules_of,
    staff_assigned,
)

pytestmark = pytest.mark.asyncio

DAY = FAR + timedelta(days=1)


def _iso(d, hour: int) -> str:
    return datetime.combine(d, datetime.min.time()).replace(hour=hour).strftime("%Y-%m-%dT%H:%M")


async def _occ(staff, pattern_id: str, day, *, action="edit", patch=None):
    async with admin_session(staff) as (db, admin):
        return await svc.materialize_occurrence(
            db, organization_id=staff["organization_id"], actor=admin,
            pattern_id=UUID(pattern_id), occurrence_date=day, action=action, patch=patch,
        )


async def _revert(staff, entry_id: str):
    async with admin_session(staff) as (db, admin):
        return await svc.revert_to_pattern(
            db, organization_id=staff["organization_id"], entry_id=UUID(entry_id), actor=admin,
        )


@pytest.fixture
def edit_patch():
    return ScheduleUpdate(start_at=_iso(DAY, 10), end_at=_iso(DAY, 18), force=True)


class TestEdit:
    async def test_edit_materializes_with_stamp_and_override(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        resp = await _occ(staff_assigned, g.blocks[0].id, DAY, patch=edit_patch)
        assert resp.status == "confirmed"
        assert resp.pattern_id == g.blocks[0].id and resp.pattern_occurrence_date == DAY
        assert resp.pattern_overridden is True
        assert resp.start_time == "10:00" and resp.end_time == "18:00"
        rows = await schedules_of(staff_assigned)
        assert len(rows) == 1 and rows[0].pattern_overridden is True

    async def test_slot_is_unique_even_for_deleted(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        async with async_session() as db:
            db.add(Schedule(
                organization_id=staff_assigned["organization_id"], user_id=staff_assigned["id"],
                store_id=staff_assigned["store_id"], operating_day=DAY,
                start_at=datetime.combine(DAY, datetime.min.time()).replace(hour=9),
                end_at=datetime.combine(DAY, datetime.min.time()).replace(hour=17),
                status="confirmed", pattern_id=UUID(g.blocks[0].id), pattern_occurrence_date=DAY,
            ))
            with pytest.raises(IntegrityError) as ei:
                await db.commit()
            assert "uq_schedules_pattern_occurrence" in str(ei.value)
            await db.rollback()

    async def test_second_edit_applies_to_same_row(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        first = await _occ(staff_assigned, g.blocks[0].id, DAY, patch=edit_patch)
        second = await _occ(staff_assigned, g.blocks[0].id, DAY,
                            patch=ScheduleUpdate(start_at=_iso(DAY, 11), end_at=_iso(DAY, 15), force=True))
        assert first.id == second.id and second.start_time == "11:00"
        assert len(await schedules_of(staff_assigned)) == 1

    async def test_edit_on_non_pattern_day_is_400(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=[1])])
        from tests.integration.api.console.test_pattern_groups import next_dow
        sunday = next_dow(FAR, 0)
        with pytest.raises(AppError):
            await _occ(staff_assigned, g.blocks[0].id, sunday, patch=edit_patch)

    async def test_edit_can_move_the_date_keeping_occurrence_date(self, staff_assigned):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        moved = DAY + timedelta(days=1)
        resp = await _occ(staff_assigned, g.blocks[0].id, DAY, patch=ScheduleUpdate(
            operating_day=moved, start_at=_iso(moved, 9), end_at=_iso(moved, 17), force=True,
        ))
        assert resp.operating_day == moved and resp.pattern_occurrence_date == DAY


class TestDelete:
    async def test_delete_materializes_as_deleted_and_occupies_slot(self, staff_assigned):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        resp = await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        assert resp.status == "deleted" and resp.pattern_overridden is True
        # 재실체화 시도 = no-op (슬롯 점유)
        async with async_session() as db:
            n = await materialize_window(
                db, organization_id=staff_assigned["organization_id"], user_ids=[staff_assigned["id"]],
                date_from=DAY, date_to=DAY,
            )
        assert n == 0
        rows = await schedules_of(staff_assigned)
        assert len(rows) == 1 and rows[0].status == "deleted"

    async def test_delete_twice_is_noop(self, staff_assigned):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        a = await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        b = await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        assert a.id == b.id and b.status == "deleted"

    async def test_edit_after_delete_is_refused(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        with pytest.raises(AppError):
            await _occ(staff_assigned, g.blocks[0].id, DAY, patch=edit_patch)


class TestRevert:
    async def test_revert_restores_pattern_values(self, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        edited = await _occ(staff_assigned, g.blocks[0].id, DAY, patch=edit_patch)
        reverted = await _revert(staff_assigned, edited.id)
        assert reverted.start_time == "09:00" and reverted.end_time == "17:00"
        assert reverted.pattern_overridden is False and reverted.pattern_id == g.blocks[0].id
        assert reverted.status == "confirmed"

    async def test_revert_via_http(self, async_client, admin_headers, staff_assigned, edit_patch):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        edited = await _occ(staff_assigned, g.blocks[0].id, DAY, patch=edit_patch)
        resp = await async_client.post(f"/api/v1/console/schedules/{edited.id}/revert-to-pattern", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pattern_overridden"] is False and resp.json()["start_time"] == "09:00"

    async def test_revert_not_overridden_is_409(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        row = (await schedules_of(staff_assigned))[0]
        assert row.pattern_overridden is False
        with pytest.raises(AppError) as ei:
            await _revert(staff_assigned, str(row.id))
        assert ei.value.status_code == 409 and ei.value.detail["code"] == "PATTERN_REVERT_NOT_OVERRIDDEN"

    async def test_revert_deleted_is_409(self, staff_assigned):
        g = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        deleted = await _occ(staff_assigned, g.blocks[0].id, DAY, action="delete")
        with pytest.raises(AppError) as ei:
            await _revert(staff_assigned, deleted.id)
        assert ei.value.detail["code"] == "PATTERN_REVERT_NOT_OVERRIDDEN"

    async def test_revert_one_time_row_is_409(self, async_client, admin_headers, staff_assigned):
        resp = await async_client.post("/api/v1/console/schedules", headers=admin_headers, json={
            "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
            "work_date": DAY.isoformat(), "start_time": "09:00", "end_time": "17:00", "force": True,
        })
        assert resp.status_code == 201
        r2 = await async_client.post(f"/api/v1/console/schedules/{resp.json()['id']}/revert-to-pattern", headers=admin_headers)
        assert r2.status_code == 409, r2.text


class TestDuplicateHasNoStamp:
    async def test_create_entry_without_stamp_is_one_time(self, staff_assigned):
        g = await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        src = (await schedules_of(staff_assigned))[0]
        assert src.pattern_id is not None
        # 같은 값으로 "복사" — create_entry 에 pattern_stamp 없음 → 도장 없음
        async with admin_session(staff_assigned) as (db, admin):
            copy = await schedule_service.create_entry(
                db, staff_assigned["organization_id"],
                ScheduleCreate(
                    user_id=str(src.user_id), store_id=str(src.store_id),
                    operating_day=src.operating_day + timedelta(days=30),
                    start_at=_iso(src.operating_day + timedelta(days=30), 9),
                    end_at=_iso(src.operating_day + timedelta(days=30), 17),
                    force=True,
                ),
                admin.id,
            )
        assert copy.pattern_id is None and copy.pattern_occurrence_date is None
        assert copy.pattern_overridden is False
        async with async_session() as db:
            row = await db.scalar(select(Schedule).where(Schedule.id == UUID(copy.id)))
        assert row.pattern_id is None and row.pattern_occurrence_date is None
