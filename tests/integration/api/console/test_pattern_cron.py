"""고정 근무 — cron 창 밀기 / catch-up / 이벤트 즉시 실체화 / 퇴사 cleanup / 라우터 스모크 (계약 §7 test_pattern_cron).

- weekly 창 밀기: 멱등(두 번 실행 = 동일 행수), 빠진 슬롯만 채움, 시각·요일 게이트
- daily catch-up: 평소 0건
- create_group(POST) 직후 창 안 행 존재(D-g 즉시 실체화) + 알림 1건(건별 아님)
- 퇴사(offboard) → cleanup_future 훅: 퇴사일 이후 미손댐 자동생성분 삭제, overridden 행은 보존
- 라우터 7개 스모크: 인증 401 / 스코프 404 / 정상
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.alert import Alert
from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.models.user import User
from app.schemas.schedule import ScheduleUpdate
from app.services.fixed_schedule import patterns as svc
from app.services.fixed_schedule.materialize import (
    FIXED_TICK_HOUR,
    FIXED_WEEKLY_DOW,
    cleanup_future,
    run_daily_catchup_tick,
    run_weekly_window_tick,
)
from tests.integration.api.console.test_pattern_groups import (  # noqa: F401
    ALL_DAYS,
    FAR,
    TODAY,
    admin_session,
    block,
    create,
    rows_of,
    schedules_of,
    staff_assigned,
)

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/console/schedules/patterns"
WINDOW_ROWS = 15  # today..today+14 (2주 창, 양끝 포함)


def _iso(d, hour: int) -> str:
    return datetime.combine(d, datetime.min.time()).replace(hour=hour).strftime("%Y-%m-%dT%H:%M")


async def _hard_delete_last(staff, n: int) -> list:
    """창 안 실 행 중 뒤에서 n개를 DB 에서 통째로 지운다(슬롯 비움 — 놓친 실체화를 흉내)."""
    rows = await schedules_of(staff)
    victims = rows[-n:]
    async with async_session() as db:
        await db.execute(delete(Schedule).where(Schedule.id.in_([r.id for r in victims])))
        await db.commit()
    return [r.operating_day for r in victims]


async def _alerts(staff) -> list[Alert]:
    async with async_session() as db:
        return list((await db.execute(
            select(Alert).where(Alert.user_id == staff["id"], Alert.type == "fixed_schedule_changed")
        )).scalars())


@pytest_asyncio.fixture
async def clean_alerts(test_user) -> AsyncIterator[None]:
    async def _wipe():
        async with async_session() as db:
            await db.execute(delete(Alert).where(
                Alert.user_id == test_user["id"], Alert.type == "fixed_schedule_changed",
            ))
            await db.commit()
    await _wipe()
    try:
        yield
    finally:
        await _wipe()


def _payload(staff, *, start_date=TODAY, blocks=None, **kw) -> dict:
    return {
        "user_id": str(staff["id"]), "store_id": str(staff["store_id"]),
        "start_date": start_date.isoformat(), "until_date": None,
        "blocks": blocks or [{"start_time": "09:00", "end_time": "17:00", "byday": ALL_DAYS}],
        **kw,
    }


# ─── cron ──────────────────────────────────────────────────────


class TestWeeklyTick:
    async def test_is_idempotent_and_fills_only_missing_slots(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        assert len(await schedules_of(staff_assigned)) == WINDOW_ROWS
        gone = await _hard_delete_last(staff_assigned, 5)
        assert len(await schedules_of(staff_assigned)) == WINDOW_ROWS - 5

        created = await run_weekly_window_tick(now_hour=FIXED_TICK_HOUR, now_dow=FIXED_WEEKLY_DOW, today=TODAY)
        assert created == 5
        rows = await schedules_of(staff_assigned)
        assert len(rows) == WINDOW_ROWS
        refilled = [r for r in rows if r.operating_day in gone]
        assert len(refilled) == 5
        assert all(r.status == "confirmed" and r.pattern_id and not r.pattern_overridden for r in refilled)
        assert all(r.created_by is None for r in refilled)  # system actor

        again = await run_weekly_window_tick(now_hour=FIXED_TICK_HOUR, now_dow=FIXED_WEEKLY_DOW, today=TODAY)
        assert again == 0
        assert len(await schedules_of(staff_assigned)) == WINDOW_ROWS

    async def test_skips_when_not_tick_hour_or_not_week_start(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        await _hard_delete_last(staff_assigned, 3)
        assert await run_weekly_window_tick(now_hour=FIXED_TICK_HOUR + 1, now_dow=FIXED_WEEKLY_DOW, today=TODAY) == 0
        assert await run_weekly_window_tick(now_hour=FIXED_TICK_HOUR, now_dow=(FIXED_WEEKLY_DOW + 3) % 7, today=TODAY) == 0
        assert len(await schedules_of(staff_assigned)) == WINDOW_ROWS - 3

    async def test_emits_no_per_day_alerts(self, staff_assigned, clean_alerts):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        await _hard_delete_last(staff_assigned, 4)
        await run_weekly_window_tick(now_hour=FIXED_TICK_HOUR, now_dow=FIXED_WEEKLY_DOW, today=TODAY)
        assert await _alerts(staff_assigned) == []  # 서비스 직접 호출 + cron = 알림 0 (알림은 라우터 몫)


class TestDailyCatchup:
    async def test_zero_when_window_already_filled(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        assert await run_daily_catchup_tick(now_hour=FIXED_TICK_HOUR, today=TODAY) == 0
        assert len(await schedules_of(staff_assigned)) == WINDOW_ROWS

    async def test_refills_missed_slot_any_weekday(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        await _hard_delete_last(staff_assigned, 1)
        assert await run_daily_catchup_tick(now_hour=FIXED_TICK_HOUR, today=TODAY) == 1
        assert await run_daily_catchup_tick(now_hour=FIXED_TICK_HOUR, today=TODAY) == 0

    async def test_future_group_outside_window_stays_virtual(self, staff_assigned):
        await create(staff_assigned, start_date=FAR, blocks=[block(byday=ALL_DAYS)])
        assert await run_daily_catchup_tick(now_hour=FIXED_TICK_HOUR, today=TODAY) == 0
        assert await schedules_of(staff_assigned) == []


# ─── 이벤트 즉시 실체화 + 알림 1건 ─────────────────────────────────


class TestImmediateMaterialize:
    async def test_post_creates_window_rows_and_one_alert(self, async_client, admin_headers, staff_assigned, clean_alerts):
        resp = await async_client.post(BASE, json=_payload(staff_assigned), headers=admin_headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert len(body["blocks"]) == 1 and body["user_id"] == str(staff_assigned["id"])
        rows = await schedules_of(staff_assigned)
        assert len(rows) == WINDOW_ROWS
        assert all(r.pattern_id == UUID(body["blocks"][0]["id"]) for r in rows)
        alerts = await _alerts(staff_assigned)
        assert len(alerts) == 1  # 작업 1회 = 알림 1건 (15건 아님)
        assert alerts[0].reference_type == "pattern_group" and str(alerts[0].reference_id) == body["group_id"]
        assert "Sun, Mon, Tue, Wed, Thu, Fri, Sat 09:00-17:00" in alerts[0].message
        # 건별 schedule_assigned 도 나가지 않는다
        async with async_session() as db:
            assigned = await db.scalar(select(Alert).where(
                Alert.user_id == staff_assigned["id"], Alert.type == "schedule_assigned",
                Alert.reference_id.in_([r.id for r in rows]),
            ))
        assert assigned is None


# ─── 퇴사 → cleanup_future ────────────────────────────────────────


@pytest_asyncio.fixture
async def restorable_member(staff_assigned, restore_pins) -> AsyncIterator[dict]:
    """offboard 는 OrgMember/User 를 바꾼다 — 테스트 뒤 원복."""
    uid, org_id = staff_assigned["id"], staff_assigned["organization_id"]
    async with async_session() as db:
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))
        prev_status = member.status if member else None
        if member:
            member.status = "active"
            member.termination_date = None
        (await db.get(User, uid)).is_active = True
        await db.commit()
    try:
        yield staff_assigned
    finally:
        async with async_session() as db:
            # unassign 된 행은 user_id 가 NULL 이라 staff_assigned 의 wipe 에 안 걸린다 — 여기서 지운다
            await db.execute(delete(Schedule).where(
                Schedule.store_id == staff_assigned["store_id"], Schedule.user_id.is_(None),
                Schedule.pattern_id.is_not(None),
            ))
            member = await db.scalar(select(OrgMember).where(
                OrgMember.user_id == uid, OrgMember.organization_id == org_id,
            ))
            if member:
                member.status = prev_status or "active"
                member.termination_date = None
                member.termination_reason = None
                member.rehire_eligible = None
            (await db.get(User, uid)).is_active = True
            await db.commit()


class TestCleanupFuture:
    async def test_direct_cleanup_keeps_overridden_and_past(self, staff_assigned):
        g = await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        cut = TODAY + timedelta(days=5)
        edited_day = TODAY + timedelta(days=8)
        async with admin_session(staff_assigned) as (db, admin):
            await svc.materialize_occurrence(
                db, organization_id=staff_assigned["organization_id"], actor=admin,
                pattern_id=UUID(g.blocks[0].id), occurrence_date=edited_day, action="edit",
                patch=ScheduleUpdate(start_at=_iso(edited_day, 10), end_at=_iso(edited_day, 18), force=True),
            )
            done = await cleanup_future(
                db, organization_id=staff_assigned["organization_id"], user_id=staff_assigned["id"], after=cut,
            )
        assert done == WINDOW_ROWS - 6 - 1  # cut 이후 9일 중 overridden 1일 제외
        rows = await schedules_of(staff_assigned)
        kept = [r for r in rows if r.status == "confirmed"]
        assert {r.operating_day for r in kept} == {TODAY + timedelta(days=i) for i in range(6)} | {edited_day}
        assert next(r for r in rows if r.operating_day == edited_day).pattern_overridden is True
        assert all(r.status == "deleted" for r in rows if r.operating_day > cut and r.operating_day != edited_day)

    async def test_offboard_hook_cleans_auto_rows_keeps_overridden(self, async_client, admin_headers, restorable_member):
        staff = restorable_member
        g = await create(staff, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        cut = TODAY + timedelta(days=4)
        edited_day = TODAY + timedelta(days=10)
        async with admin_session(staff) as (db, admin):
            await svc.materialize_occurrence(
                db, organization_id=staff["organization_id"], actor=admin,
                pattern_id=UUID(g.blocks[0].id), occurrence_date=edited_day, action="edit",
                patch=ScheduleUpdate(start_at=_iso(edited_day, 10), end_at=_iso(edited_day, 18), force=True),
            )
        resp = await async_client.post(
            f"/api/v1/console/users/{staff['id']}/offboard",
            json={"termination_date": cut.isoformat(), "future_schedule_action": "unassign"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        async with async_session() as db:
            rows = list((await db.execute(select(Schedule).where(
                Schedule.store_id == staff["store_id"], Schedule.pattern_id.is_not(None),
            ).order_by(Schedule.operating_day))).scalars())
        before = [r for r in rows if r.operating_day <= cut]
        assert len(before) == 5 and all(r.status == "confirmed" and r.user_id == staff["id"] for r in before)
        auto_after = [r for r in rows if r.operating_day > cut and r.operating_day != edited_day]
        # 훅(cleanup_future)이 raw unassign 보다 먼저 돌아 자동생성분은 delete_entry 로 지워진다(사람 비우기 X)
        assert auto_after and all(r.status == "deleted" and r.user_id == staff["id"] for r in auto_after)
        edited = next(r for r in rows if r.operating_day == edited_day)
        assert edited.pattern_overridden is True and edited.status == "confirmed" and edited.user_id is None


# ─── 라우터 스모크 ─────────────────────────────────────────────────


class TestRouterSmoke:
    async def test_requires_auth(self, async_client, staff_assigned):
        assert (await async_client.get(BASE, params={"user_id": str(staff_assigned["id"])})).status_code in (401, 403)
        assert (await async_client.post(BASE, json=_payload(staff_assigned))).status_code in (401, 403)

    async def test_unknown_store_is_404(self, async_client, admin_headers, staff_assigned):
        p = _payload(staff_assigned, start_date=FAR)
        p["store_id"] = str(uuid4())
        assert (await async_client.post(BASE, json=p, headers=admin_headers)).status_code == 404
        assert (await async_client.post(f"{BASE}/validate", json=p, headers=admin_headers)).status_code == 404
        assert (await async_client.get(BASE, params={"user_id": str(staff_assigned["id"]), "store_id": p["store_id"]},
                                       headers=admin_headers)).status_code == 404

    async def test_unknown_group_and_pattern_are_404(self, async_client, admin_headers, staff_assigned):
        gid = uuid4()
        assert (await async_client.patch(f"{BASE}/groups/{gid}", json=_payload(staff_assigned), headers=admin_headers)).status_code == 404
        assert (await async_client.post(f"{BASE}/groups/{gid}/move", json={"delta_days": 7}, headers=admin_headers)).status_code == 404
        assert (await async_client.delete(f"{BASE}/groups/{gid}", headers=admin_headers)).status_code == 404
        r = await async_client.post(f"{BASE}/{uuid4()}/occurrences/{FAR.isoformat()}",
                                    json={"action": "delete"}, headers=admin_headers)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "PATTERN_NOT_FOUND"

    async def test_happy_path_all_endpoints(self, async_client, admin_headers, staff_assigned, clean_alerts):
        # validate (깨끗)
        r = await async_client.post(f"{BASE}/validate", json=_payload(staff_assigned, start_date=FAR), headers=admin_headers)
        assert r.status_code == 200 and r.json() == {"errors": [], "overlaps": []}
        # validate — 블록 겹침은 errors 로
        bad = _payload(staff_assigned, start_date=FAR, blocks=[
            {"start_time": "09:00", "end_time": "17:00", "byday": [1]},
            {"start_time": "16:00", "end_time": "20:00", "byday": [1]},
        ])
        r = await async_client.post(f"{BASE}/validate", json=bad, headers=admin_headers)
        assert r.status_code == 200 and r.json()["errors"][0]["code"] == "PATTERN_BLOCK_OVERLAP"

        # create (창 밖 → 실 행 없음)
        r = await async_client.post(BASE, json=_payload(staff_assigned, start_date=FAR), headers=admin_headers)
        assert r.status_code == 201, r.text
        gid = r.json()["group_id"]
        pid = r.json()["blocks"][0]["id"]
        assert await schedules_of(staff_assigned) == []

        # list
        r = await async_client.get(BASE, params={"user_id": str(staff_assigned["id"])}, headers=admin_headers)
        assert r.status_code == 200 and [g["group_id"] for g in r.json()] == [gid]
        r = await async_client.get(BASE, params={"user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"])},
                                   headers=admin_headers)
        assert r.status_code == 200 and len(r.json()) == 1

        # overlap with existing → 409 + candidates
        r = await async_client.post(BASE, json=_payload(staff_assigned, start_date=FAR), headers=admin_headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PATTERN_OVERLAP_EXISTING"
        assert r.json()["detail"]["overlaps"][0]["group_id"] == gid

        # update (블록 교체)
        upd = _payload(staff_assigned, start_date=FAR, blocks=[{"start_time": "10:00", "end_time": "14:00", "byday": [2, 4]}])
        r = await async_client.patch(f"{BASE}/groups/{gid}", json=upd, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["blocks"][0]["byday"] == [2, 4] and r.json()["group_id"] == gid
        pid = r.json()["blocks"][0]["id"]

        # move (+7d)
        r = await async_client.post(f"{BASE}/groups/{gid}/move", json={"delta_days": 7}, headers=admin_headers)
        assert r.status_code == 200 and r.json()["start_date"] == (FAR + timedelta(days=7)).isoformat()
        # move into past → 409
        r = await async_client.post(f"{BASE}/groups/{gid}/move", json={"delta_days": -365}, headers=admin_headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PATTERN_MOVE_INTO_PAST"

        # occurrence edit — 이동 후 첫 Tue(2) 찾기
        d = FAR + timedelta(days=7)
        while (d.weekday() + 1) % 7 != 2:
            d += timedelta(days=1)
        r = await async_client.post(
            f"{BASE}/{pid}/occurrences/{d.isoformat()}",
            json={"action": "edit", "patch": {"start_at": _iso(d, 11), "end_at": _iso(d, 15), "force": True}},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pattern_id"] == pid and body["pattern_overridden"] is True and body["status"] == "confirmed"
        entry_id = body["id"]
        # occurrence on a non-pattern day → 400
        r = await async_client.post(f"{BASE}/{pid}/occurrences/{(d + timedelta(days=1)).isoformat()}",
                                    json={"action": "delete"}, headers=admin_headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "PATTERN_NO_OCCURRENCE"
        # revert-to-pattern (schedules.py)
        r = await async_client.post(f"/api/v1/console/schedules/{entry_id}/revert-to-pattern", headers=admin_headers)
        assert r.status_code == 200 and r.json()["pattern_overridden"] is False
        # occurrence delete
        r = await async_client.post(f"{BASE}/{pid}/occurrences/{d.isoformat()}", json={"action": "delete"}, headers=admin_headers)
        assert r.status_code == 200 and r.json()["status"] == "deleted"

        # delete group → 204, 행 사라짐, 실 행은 도장 해제
        r = await async_client.delete(f"{BASE}/groups/{gid}", headers=admin_headers)
        assert r.status_code == 204
        assert await rows_of(gid) == []
        r = await async_client.get(BASE, params={"user_id": str(staff_assigned["id"])}, headers=admin_headers)
        assert r.json() == []
        assert all(s.pattern_id is None for s in await schedules_of(staff_assigned))

        # 알림: create / update / move / delete = 4건 (move 실패·occurrence 는 0)
        assert len(await _alerts(staff_assigned)) == 4
