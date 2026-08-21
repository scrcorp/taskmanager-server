"""고정 근무 — 조회 시 virtual 합성 (계약 §7 test_pattern_overview_virtual).

- 창 밖은 virtual 만 (`status="virtual"`, id `virtual:<pattern_id>:<date>`)
- 도장 행(실체화)은 그 슬롯의 virtual 을 억제
- 도장 없는 실 행(일회성)은 억제하지 않는다 — 가산
- **deleted 행도 억제**(슬롯 점유)
- 퇴사(assignable_until) 뒤로는 virtual 이 소멸
- `status='virtual'` 저장은 CHECK 위반
- roster 행/컬럼/합계에 virtual 가산
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.schemas.schedule import ScheduleUpdate
from app.services.fixed_schedule import patterns as svc
from tests.integration.api.console.test_pattern_groups import (  # noqa: F401 — 픽스처 재사용
    ALL_DAYS,
    FAR,
    admin_session,
    block,
    create,
    rows_of,
    staff_assigned,
)

pytestmark = pytest.mark.asyncio

LIST_URL = "/api/v1/console/schedules"
ROSTER_URL = "/api/v1/console/schedules/roster"
WEEK_END = FAR + timedelta(days=6)


async def _list(client, headers, staff, *, date_from=FAR, date_to=WEEK_END) -> list[dict]:
    resp = await client.get(LIST_URL, headers=headers, params={
        "user_id": str(staff["id"]), "store_id": str(staff["store_id"]),
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


@pytest_asyncio.fixture
async def far_group(staff_assigned):
    """FAR 부터 매일 09–17 — 창(2주) 밖이라 실 행 0, virtual 만."""
    out = await create(staff_assigned, start_date=FAR, blocks=[block(byday=ALL_DAYS)])
    return out


async def _edit(staff, pattern_id, day: date, *, action="edit"):
    async with admin_session(staff) as (db, admin):
        patch = ScheduleUpdate(
            start_at=datetime.combine(day, datetime.min.time()).replace(hour=10).strftime("%Y-%m-%dT%H:%M"),
            end_at=datetime.combine(day, datetime.min.time()).replace(hour=18).strftime("%Y-%m-%dT%H:%M"),
            force=True,
        ) if action == "edit" else None
        from uuid import UUID
        return await svc.materialize_occurrence(
            db, organization_id=staff["organization_id"], actor=admin,
            pattern_id=UUID(pattern_id), occurrence_date=day, action=action, patch=patch,
        )


class TestVirtualList:
    async def test_outside_window_only_virtual(self, async_client, admin_headers, staff_assigned, far_group):
        items = await _list(async_client, admin_headers, staff_assigned)
        assert len(items) == 7
        pid = far_group.blocks[0].id
        for it in items:
            assert it["status"] == "virtual"
            assert it["id"] == f"virtual:{pid}:{it['operating_day']}"
            assert it["pattern_id"] == pid and it["pattern_overridden"] is False
            assert it["pattern_occurrence_date"] == it["operating_day"]
            assert it["start_time"] == "09:00" and it["end_time"] == "17:00"
            assert it["net_work_minutes"] == 480
        assert [it["operating_day"] for it in items] == [(FAR + timedelta(days=i)).isoformat() for i in range(7)]

    async def test_no_window_returns_real_rows_only(self, async_client, admin_headers, staff_assigned, far_group):
        resp = await async_client.get(LIST_URL, headers=admin_headers, params={
            "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        })
        assert resp.status_code == 200
        assert all(it["status"] != "virtual" for it in resp.json()["items"])

    async def test_stamped_row_suppresses_its_slot(self, async_client, admin_headers, staff_assigned, far_group):
        day = FAR + timedelta(days=2)
        await _edit(staff_assigned, far_group.blocks[0].id, day)
        items = await _list(async_client, admin_headers, staff_assigned)
        assert len(items) == 7
        that = [it for it in items if it["operating_day"] == day.isoformat()]
        assert len(that) == 1
        assert that[0]["status"] == "confirmed" and that[0]["pattern_overridden"] is True
        assert that[0]["start_time"] == "10:00"

    async def test_unstamped_row_is_added_not_suppressing(self, async_client, admin_headers, staff_assigned, far_group):
        day = FAR + timedelta(days=3)
        resp = await async_client.post(LIST_URL, headers=admin_headers, json={
            "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
            "work_date": day.isoformat(), "start_time": "18:00", "end_time": "21:00", "force": True,
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["pattern_id"] is None
        items = await _list(async_client, admin_headers, staff_assigned)
        that = [it for it in items if it["operating_day"] == day.isoformat()]
        assert {it["status"] for it in that} == {"virtual", "confirmed"}
        assert len(items) == 8

    async def test_deleted_row_also_suppresses(self, async_client, admin_headers, staff_assigned, far_group):
        day = FAR + timedelta(days=4)
        await _edit(staff_assigned, far_group.blocks[0].id, day, action="delete")
        items = await _list(async_client, admin_headers, staff_assigned)
        assert len(items) == 6
        assert day.isoformat() not in {it["operating_day"] for it in items}

    async def test_cost_hidden_for_supervisor(self, async_client, staff_assigned, far_group, test_users, second_store_id):
        """SV 응답의 virtual 도 effective_rate 가 가려진다."""
        from app.models.user_store import UserStore
        from sqlalchemy import delete
        sv = test_users["testsv"]
        async with async_session() as db:
            await db.execute(delete(UserStore).where(UserStore.user_id == sv["id"], UserStore.store_id == staff_assigned["store_id"]))
            db.add(UserStore(user_id=sv["id"], store_id=staff_assigned["store_id"], is_manager=True, is_work_assignment=True))
            await db.commit()
        try:
            login = await async_client.post("/api/v1/console/auth/login", json={"username": "testsv", "password": "1234"})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            items = await _list(async_client, headers, staff_assigned)
            assert items and all(it["effective_rate"] is None for it in items)
        finally:
            async with async_session() as db:
                await db.execute(delete(UserStore).where(UserStore.user_id == sv["id"], UserStore.store_id == staff_assigned["store_id"]))
                await db.commit()


class TestTermination:
    @pytest_asyncio.fixture
    async def terminated_after_two_days(self, staff_assigned) -> AsyncIterator[date]:
        last_day = FAR + timedelta(days=2)
        async with async_session() as db:
            m = await db.scalar(select(OrgMember).where(
                OrgMember.user_id == staff_assigned["id"],
                OrgMember.organization_id == staff_assigned["organization_id"],
            ))
            assert m is not None, "test staff must be an org member"
            before = (m.status, m.termination_date)
            m.status = "terminated"
            m.termination_date = last_day
            await db.commit()
        try:
            yield last_day
        finally:
            async with async_session() as db:
                m = await db.scalar(select(OrgMember).where(
                    OrgMember.user_id == staff_assigned["id"],
                    OrgMember.organization_id == staff_assigned["organization_id"],
                ))
                m.status, m.termination_date = before
                await db.commit()

    async def test_virtual_stops_after_last_working_day(
        self, async_client, admin_headers, staff_assigned, far_group, terminated_after_two_days,
    ):
        items = await _list(async_client, admin_headers, staff_assigned)
        days = [it["operating_day"] for it in items]
        assert days == [(FAR + timedelta(days=i)).isoformat() for i in range(3)]  # 퇴사일 당일까지


class TestVirtualIsResponseOnly:
    async def test_saving_status_virtual_violates_check(self, staff_assigned):
        async with async_session() as db:
            db.add(Schedule(
                organization_id=staff_assigned["organization_id"], user_id=staff_assigned["id"],
                store_id=staff_assigned["store_id"], operating_day=FAR,
                start_at=datetime.combine(FAR, datetime.min.time()).replace(hour=9),
                end_at=datetime.combine(FAR, datetime.min.time()).replace(hour=17),
                status="virtual",
            ))
            with pytest.raises(IntegrityError) as ei:
                await db.commit()
            assert "ck_schedules_no_virtual" in str(ei.value)
            await db.rollback()


class TestRoster:
    async def test_roster_counts_virtual_as_confirmed(self, async_client, admin_headers, staff_assigned, far_group):
        resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
            "date_from": FAR.isoformat(), "date_to": WEEK_END.isoformat(),
            "store_ids": str(staff_assigned["store_id"]), "staff_ids": str(staff_assigned["id"]),
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        row = next(r for r in body["roster"] if r["user_id"] == str(staff_assigned["id"]))
        assert row["has_schedule_in_period"] is True
        assert row["confirmed_hours"] == 56.0
        assert body["totals"]["team_confirmed"] == 7 and body["totals"]["hours_confirmed"] == 56.0
        assert body["totals"]["staff_count"] >= 1
        cols = {c["key"]: c for c in body["columns"]}
        assert all(cols[(FAR + timedelta(days=i)).isoformat()]["team_confirmed"] == 1 for i in range(7))

    async def test_roster_does_not_double_count_materialized_slot(self, async_client, admin_headers, staff_assigned, far_group):
        await _edit(staff_assigned, far_group.blocks[0].id, FAR)
        resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
            "date_from": FAR.isoformat(), "date_to": WEEK_END.isoformat(),
            "store_ids": str(staff_assigned["store_id"]), "staff_ids": str(staff_assigned["id"]),
        })
        body = resp.json()
        assert body["totals"]["team_confirmed"] == 7
        cols = {c["key"]: c for c in body["columns"]}
        assert cols[FAR.isoformat()]["team_confirmed"] == 1
