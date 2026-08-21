"""고정 근무 — 개별 스케줄 쓰기 시 패턴 겹침 경고 + availability (계약 §7 test_pattern_validation).

- `POST /schedules` 가 해당 날짜의 virtual 과 겹치면 OVERLAPPING_SCHEDULE + `source:"pattern"` 경고(409),
  force 로 저장(201, 응답 warnings 에 동봉)
- 시간이 안 겹치면 패턴 경고 없음
- 슬롯이 이미 실 행(실체화/deleted)이면 virtual 이 아니므로 패턴 경고 없음
- 패턴 availability: 미설정=통과 / off=400 (`validate_group` 도 같은 결과) — 일반 스케줄엔 적용 안 함
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from app.core import schedule_codes as codes
from app.database import async_session
from app.services.fixed_schedule import patterns as svc
from app.utils.exceptions import AppError
from tests.integration.api.console.test_pattern_groups import (  # noqa: F401
    ALL_DAYS,
    FAR,
    _set_availability,
    admin_session,
    block,
    create,
    group_in,
    staff_assigned,
)

pytestmark = pytest.mark.asyncio

URL = "/api/v1/console/schedules"
DAY = FAR + timedelta(days=2)


def _payload(staff, day, start="10:00", end="14:00", **over):
    base = {
        "user_id": str(staff["id"]), "store_id": str(staff["store_id"]),
        "work_date": day.isoformat(), "start_time": start, "end_time": end, "status": "confirmed",
    }
    base.update(over)
    return base


def _detail(resp) -> dict:
    return resp.json().get("detail") or resp.json().get("error") or {}


def _pattern_warnings(warnings: list[dict]) -> list[dict]:
    return [w for w in warnings if w["code"] == codes.OVERLAPPING_SCHEDULE and w["params"].get("source") == "pattern"]


class TestOneTimeWriteVsPattern:
    async def test_overlap_with_virtual_is_warning_with_source_pattern(self, async_client, admin_headers, staff_assigned):
        g = await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY))
        assert resp.status_code == 409, resp.text
        d = _detail(resp)
        assert d["code"] == codes.SCHEDULE_WARNINGS_UNCONFIRMED
        pw = _pattern_warnings(d["warnings"])
        assert len(pw) == 1
        assert pw[0]["params"]["pattern_id"] == g.blocks[0].id
        assert pw[0]["params"]["occurrence_date"] == DAY.isoformat()

    async def test_force_saves_and_echoes_warning(self, async_client, admin_headers, staff_assigned):
        await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY, force=True))
        assert resp.status_code == 201, resp.text
        assert resp.json()["pattern_id"] is None

    async def test_non_overlapping_time_has_no_pattern_warning(self, async_client, admin_headers, staff_assigned):
        await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY, "18:00", "21:00"))
        assert resp.status_code == 201, resp.text

    async def test_overnight_virtual_overlaps_next_day_early_shift(self, async_client, admin_headers, staff_assigned):
        """전날 22–06 패턴은 다음 영업일 새벽 일회성과 물리적으로 겹친다 — ±1일 범위로 본다."""
        await create(staff_assigned, blocks=[block("22:00", "06:00", ALL_DAYS)])
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY, "05:00", "09:00"))
        assert resp.status_code == 409, resp.text
        assert _pattern_warnings(_detail(resp)["warnings"])

    async def test_materialized_slot_is_not_a_pattern_warning(self, async_client, admin_headers, staff_assigned):
        """실체화된 슬롯은 실 행 — 실 행 겹침 경고(source 없음)만 나오고 pattern 경고는 없다."""
        g = await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        async with admin_session(staff_assigned) as (db, admin):
            from app.schemas.schedule import ScheduleUpdate
            await svc.materialize_occurrence(
                db, organization_id=staff_assigned["organization_id"], actor=admin,
                pattern_id=UUID(g.blocks[0].id), occurrence_date=DAY, action="edit",
                patch=ScheduleUpdate(note="kept", force=True),
            )
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY))
        assert resp.status_code == 409, resp.text
        ws = _detail(resp)["warnings"]
        assert any(w["code"] == codes.OVERLAPPING_SCHEDULE for w in ws)
        assert not _pattern_warnings(ws)

    async def test_deleted_slot_has_no_pattern_warning(self, async_client, admin_headers, staff_assigned):
        g = await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        async with admin_session(staff_assigned) as (db, admin):
            await svc.materialize_occurrence(
                db, organization_id=staff_assigned["organization_id"], actor=admin,
                pattern_id=UUID(g.blocks[0].id), occurrence_date=DAY, action="delete", patch=None,
            )
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, DAY))
        assert resp.status_code == 201, resp.text

    async def test_materializing_itself_does_not_self_warn(self, staff_assigned):
        """실체화가 자기 virtual 과 겹친다고 기록되면 안 된다 — acknowledged_warnings 에 pattern 경고 없음."""
        from sqlalchemy import select
        from app.models.schedule import ScheduleAuditLog
        from tests.integration.api.console.test_pattern_groups import TODAY, schedules_of
        await create(staff_assigned, start_date=TODAY, blocks=[block("09:00", "17:00", ALL_DAYS)])
        row = (await schedules_of(staff_assigned))[0]
        async with async_session() as db:
            log = await db.scalar(select(ScheduleAuditLog).where(ScheduleAuditLog.schedule_id == row.id))
        assert not _pattern_warnings(log.acknowledged_warnings or [])

    async def test_preview_endpoint_reports_pattern_overlap(self, async_client, admin_headers, staff_assigned):
        g = await create(staff_assigned, blocks=[block("09:00", "17:00", ALL_DAYS)])
        resp = await async_client.post(f"{URL}/validate", headers=admin_headers, json=_payload(staff_assigned, DAY))
        assert resp.status_code == 200, resp.text
        pw = _pattern_warnings(resp.json()["warnings"])
        assert pw and pw[0]["params"]["pattern_id"] == g.blocks[0].id


class TestPatternAvailability:
    async def test_unset_passes(self, staff_assigned):
        assert (await create(staff_assigned, blocks=[block(byday=[2])])).group_id

    async def test_off_is_400(self, staff_assigned):
        await _set_availability(staff_assigned, [(2, "off", None, None)])
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, blocks=[block(byday=[2])])
        assert ei.value.status_code == 400 and ei.value.detail["code"] == "PATTERN_OUTSIDE_AVAILABILITY"

    async def test_validate_group_lists_issues_without_saving(self, staff_assigned):
        await _set_availability(staff_assigned, [(2, "off", None, None)])
        other = await create(staff_assigned, blocks=[block("09:00", "17:00", [4])])
        async with async_session() as db:
            out = await svc.validate_group(
                db, organization_id=staff_assigned["organization_id"],
                data=group_in(staff_assigned, blocks=[
                    block("09:00", "12:00", [2, 4]), block("11:00", "15:00", [2]),
                ]),
            )
        codes_ = [e.code for e in out.errors]
        assert "PATTERN_BLOCK_OVERLAP" in codes_ and "PATTERN_OUTSIDE_AVAILABILITY" in codes_
        assert [g.group_id for g in out.overlaps] == [other.group_id]
        from tests.integration.api.console.test_pattern_groups import rows_of
        async with async_session() as db:
            from sqlalchemy import select
            from app.models.work_pattern import StaffWorkPattern
            n = len((await db.execute(select(StaffWorkPattern).where(
                StaffWorkPattern.user_id == staff_assigned["id"]))).scalars().all())
        assert n == len(await rows_of(other.group_id))  # 저장 없음

    async def test_one_time_schedule_not_blocked_by_availability(self, async_client, admin_headers, staff_assigned):
        from tests.integration.api.console.test_pattern_groups import next_dow
        await _set_availability(staff_assigned, [(2, "off", None, None)])
        day = next_dow(FAR, 2)
        resp = await async_client.post(URL, headers=admin_headers, json=_payload(staff_assigned, day, force=True))
        assert resp.status_code == 201, resp.text
