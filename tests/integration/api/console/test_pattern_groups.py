"""고정 근무 패턴 그룹 — 생성/검증/수정/이동/삭제 (계약 §7 test_pattern_groups).

- 생성: 블록 N → 행 N, group_id 동일
- ① 창 안 블록 겹침 → 400 PATTERN_BLOCK_OVERLAP
- ② 다른 그룹과 겹침 → 409 PATTERN_OVERLAP_EXISTING / gate=move / gate=replace
- ④ availability off → 400 PATTERN_OUTSIDE_AVAILABILITY, 미설정 = 통과
- update: 시작 전(그대로 교체) / 진행 중(옛 행 until=today-1, 새 행 start=today)
- move: 델타 / 과거 → 409 PATTERN_MOVE_INTO_PAST / 진행 중 → 409 PATTERN_GROUP_STARTED
- delete: 기존 실 행 pattern_id NULL + confirmed 유지 (미래 자동생성분은 삭제)

패턴 서비스는 `app.services.fixed_schedule.patterns` 를 직접 호출한다(라우터는 server-api-cron 몫).
이 파일의 픽스처/헬퍼는 다른 test_pattern_* 가 import 해 쓴다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.attendance import Attendance
from app.models.availability import StaffAvailability
from app.models.schedule import Schedule
from app.models.user import User
from app.models.user_store import UserStore
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule_pattern import PatternBlockIn, PatternGroupIn
from app.services.fixed_schedule import patterns as svc
from app.services.fixed_schedule.expand import dow_sun0
from app.utils.exceptions import AppError

pytestmark = pytest.mark.asyncio

TODAY = date.today()
FAR = TODAY + timedelta(days=60)  # 창(2주) 밖 — virtual 로만 존재
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


# ─── 공용 픽스처/헬퍼 (다른 test_pattern_* 가 import) ────────────


@pytest_asyncio.fixture
async def staff_assigned(test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 test_store 에 work 배정 + 패턴/스케줄 전후 정리."""
    async with async_session() as db:
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
        ))
        db.add(UserStore(user_id=test_user["id"], store_id=test_store_id,
                         is_manager=False, is_work_assignment=True))
        await db.commit()
    await _wipe(test_user["id"], test_store_id)
    try:
        yield {**test_user, "store_id": test_store_id}
    finally:
        await _wipe(test_user["id"], test_store_id)
        async with async_session() as db:
            await db.execute(delete(UserStore).where(
                UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
            ))
            await db.commit()


async def _wipe(user_id: UUID, store_id: UUID) -> None:
    """테스트 매장 범위만 지운다 — 다른 매장의 기존 행(근태 FK 연결)은 건드리지 않는다."""
    async with async_session() as db:
        # 근태는 스케줄 FK SET NULL 로 풀리면 walk-in 유니크에 걸린다 — conftest 와 같은 순서로 먼저 지운다
        await db.execute(delete(Attendance).where(Attendance.user_id == user_id, Attendance.store_id == store_id))
        await db.execute(delete(Schedule).where(Schedule.user_id == user_id, Schedule.store_id == store_id))
        await db.execute(delete(StaffWorkPattern).where(StaffWorkPattern.user_id == user_id))
        await db.execute(delete(StaffAvailability).where(StaffAvailability.user_id == user_id))
        await db.commit()


@asynccontextmanager
async def admin_session(staff: dict) -> AsyncIterator[tuple[AsyncSession, User]]:
    """(db, testadmin) — actor 는 **같은 세션**에서 role 까지 eager 로드한다.

    서비스가 audit 에서 `actor.role` 을 읽는데, 다른 세션에서 온 User 나 role 미로드 인스턴스는
    async 세션에서 lazy load 가 불가능하다(greenlet 에러). API 경로는 get_current_user 가 같은
    요청 세션에 selectinload 로 올려두므로 문제가 없다 — 서비스 직접 호출 테스트만 이 모양이 필요.
    """
    async with async_session() as db:
        admin = await db.scalar(
            select(User).options(selectinload(User.role)).where(
                User.username == "testadmin", User.organization_id == staff["organization_id"],
            )
        )
        assert admin is not None
        yield db, admin


def block(start="09:00", end="17:00", byday=None, **kw) -> PatternBlockIn:
    return PatternBlockIn(start_time=start, end_time=end, byday=byday or [1, 3, 5], **kw)


def group_in(staff, *, start_date=FAR, until_date=None, blocks=None, gate=None) -> PatternGroupIn:
    return PatternGroupIn(
        user_id=str(staff["id"]), store_id=str(staff["store_id"]),
        start_date=start_date, until_date=until_date,
        blocks=blocks or [block()], gate=gate,
    )


async def create(staff, **kw):
    async with admin_session(staff) as (db, admin):
        return await svc.create_group(db, organization_id=staff["organization_id"], actor=admin, data=group_in(staff, **kw))


async def rows_of(group_id: str | UUID) -> list[StaffWorkPattern]:
    async with async_session() as db:
        return list((await db.execute(
            select(StaffWorkPattern).where(StaffWorkPattern.group_id == UUID(str(group_id)))
            .order_by(StaffWorkPattern.start_date, StaffWorkPattern.start_time)
        )).scalars())


async def schedules_of(staff: dict, *, include_deleted: bool = True) -> list[Schedule]:
    """테스트 매장 범위의 실 행 (다른 매장의 기존 행 제외)."""
    async with async_session() as db:
        q = select(Schedule).where(
            Schedule.user_id == staff["id"], Schedule.store_id == staff["store_id"],
        ).order_by(Schedule.operating_day)
        if not include_deleted:
            q = q.where(Schedule.status != "deleted")
        return list((await db.execute(q)).scalars())


def next_dow(start: date, dow: int) -> date:
    """start 이후(포함) 첫 번째 dow(0=Sun) 날짜."""
    d = start
    while dow_sun0(d) != dow:
        d += timedelta(days=1)
    return d


def code_of(exc: AppError) -> str:
    return exc.detail["code"]


# ─── 생성 ───────────────────────────────────────────────────────


class TestCreate:
    async def test_blocks_become_rows_sharing_group_id(self, staff_assigned):
        out = await create(staff_assigned, blocks=[
            block("09:00", "13:00", [1, 3]), block("14:00", "18:00", [1, 3]), block("10:00", "16:00", [6]),
        ])
        rows = await rows_of(out.group_id)
        assert len(rows) == 3
        assert {str(r.group_id) for r in rows} == {out.group_id}
        assert {r.rrule for r in rows} == {"FREQ=WEEKLY;BYDAY=MO,WE", "FREQ=WEEKLY;BYDAY=SA"}
        assert [b.id for b in out.blocks] == [str(r.id) for r in rows]
        assert out.start_date == FAR and out.until_date is None

    async def test_future_group_outside_window_creates_no_rows(self, staff_assigned):
        await create(staff_assigned)
        assert await schedules_of(staff_assigned) == []

    async def test_group_starting_today_is_materialized_in_window(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY, blocks=[block(byday=ALL_DAYS)])
        rows = await schedules_of(staff_assigned)
        assert len(rows) == 15  # today..today+14 (2주 창, 양끝 포함)
        assert all(r.status == "confirmed" and r.pattern_id and not r.pattern_overridden for r in rows)
        assert all(r.pattern_occurrence_date == r.operating_day for r in rows)

    async def test_block_period_override_and_two_shifts_same_day(self, staff_assigned):
        later = FAR + timedelta(days=14)
        out = await create(staff_assigned, blocks=[
            block("09:00", "13:00", [2]), block("13:00", "17:00", [2], start_date=later),
        ])
        rows = await rows_of(out.group_id)
        assert [r.start_date for r in rows] == [FAR, later]


# ─── ① 블록 겹침 ────────────────────────────────────────────────


class TestBlockOverlap:
    async def test_same_dow_overlapping_times_is_400(self, staff_assigned):
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, blocks=[
                block("09:00", "17:00", [1, 2]), block("16:00", "20:00", [2, 4]),
            ])
        assert ei.value.status_code == 400
        assert code_of(ei.value) == "PATTERN_BLOCK_OVERLAP"
        assert ei.value.detail["blocks"] == [0, 1] and ei.value.detail["dow"] == 2

    async def test_overnight_block_overlaps_next_morning_block_same_dow(self, staff_assigned):
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, blocks=[
                block("22:00", "06:00", [5]), block("22:30", "23:30", [5]),
            ])
        assert code_of(ei.value) == "PATTERN_BLOCK_OVERLAP"

    async def test_back_to_back_shifts_allowed(self, staff_assigned):
        out = await create(staff_assigned, blocks=[
            block("09:00", "13:00", [1]), block("13:00", "17:00", [1]),
        ])
        assert len(out.blocks) == 2

    async def test_different_dow_allowed(self, staff_assigned):
        out = await create(staff_assigned, blocks=[
            block("09:00", "17:00", [1]), block("09:00", "17:00", [2]),
        ])
        assert len(out.blocks) == 2


# ─── ② 다른 그룹과 겹침 + gate ──────────────────────────────────


class TestExistingOverlap:
    async def test_without_gate_409_with_candidates(self, staff_assigned):
        first = await create(staff_assigned)
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, start_date=FAR - timedelta(days=7))
        assert ei.value.status_code == 409
        assert code_of(ei.value) == "PATTERN_OVERLAP_EXISTING"
        assert [g["group_id"] for g in ei.value.detail["overlaps"]] == [first.group_id]

    async def test_non_intersecting_period_is_not_overlap(self, staff_assigned):
        await create(staff_assigned, until_date=FAR + timedelta(days=6))
        out = await create(staff_assigned, start_date=FAR + timedelta(days=7))
        assert out.group_id

    async def test_disjoint_dow_is_not_overlap(self, staff_assigned):
        await create(staff_assigned, blocks=[block(byday=[1, 3])])
        out = await create(staff_assigned, blocks=[block(byday=[2, 4])])
        assert out.group_id

    async def test_gate_move_shifts_existing_start_only(self, staff_assigned):
        first = await create(staff_assigned, blocks=[block("09:00", "17:00")])
        earlier = FAR - timedelta(days=7)
        out = await create(staff_assigned, start_date=earlier,
                           blocks=[block("10:00", "18:00")], gate="move")
        assert out.group_id == first.group_id
        rows = await rows_of(first.group_id)
        assert [r.start_date for r in rows] == [earlier]
        assert rows[0].start_time.hour == 9  # 기존 설정 유지
        async with async_session() as db:
            total = (await db.execute(select(StaffWorkPattern).where(
                StaffWorkPattern.user_id == staff_assigned["id"]))).scalars().all()
        assert len(total) == 1  # 신규 생성 없음

    async def test_gate_move_on_started_group_is_409(self, staff_assigned):
        await create(staff_assigned, start_date=TODAY - timedelta(days=7))
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, start_date=TODAY, gate="move")
        assert code_of(ei.value) == "PATTERN_GROUP_STARTED"

    async def test_gate_replace_deletes_existing_and_creates(self, staff_assigned):
        first = await create(staff_assigned)
        out = await create(staff_assigned, blocks=[block("10:00", "18:00")], gate="replace")
        assert out.group_id != first.group_id
        assert await rows_of(first.group_id) == []
        assert (await rows_of(out.group_id))[0].start_time.hour == 10


# ─── ④ availability ─────────────────────────────────────────────


async def _set_availability(staff, rows: list[tuple[int, str, str | None, str | None]]):
    from datetime import time as _time
    async with async_session() as db:
        await db.execute(delete(StaffAvailability).where(StaffAvailability.user_id == staff["id"]))
        for dow, state, s, e in rows:
            db.add(StaffAvailability(
                user_id=staff["id"], organization_id=staff["organization_id"], day_of_week=dow,
                state=state, source="console_manager",
                start_time=_time.fromisoformat(s) if s else None,
                end_time=_time.fromisoformat(e) if e else None,
            ))
        await db.commit()


class TestAvailability:
    async def test_no_rows_means_no_constraint(self, staff_assigned):
        assert (await create(staff_assigned)).group_id

    async def test_missing_dow_row_means_no_constraint(self, staff_assigned):
        await _set_availability(staff_assigned, [(0, "off", None, None)])  # Sun off 만 — Mon/Wed/Fri 는 행 없음
        assert (await create(staff_assigned, blocks=[block(byday=[1, 3, 5])])).group_id

    async def test_off_day_is_400(self, staff_assigned):
        await _set_availability(staff_assigned, [(3, "off", None, None)])
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, blocks=[block(byday=[1, 3])])
        assert ei.value.status_code == 400
        assert code_of(ei.value) == "PATTERN_OUTSIDE_AVAILABILITY"
        assert ei.value.detail["dow"] == 3 and ei.value.detail["block"] == 0

    async def test_outside_range_is_400_inside_passes(self, staff_assigned):
        await _set_availability(staff_assigned, [(1, "range", "10:00", "18:00")])
        with pytest.raises(AppError) as ei:
            await create(staff_assigned, blocks=[block("09:00", "17:00", [1])])
        assert code_of(ei.value) == "PATTERN_OUTSIDE_AVAILABILITY"
        assert (await create(staff_assigned, blocks=[block("10:00", "17:00", [1])])).group_id

    async def test_full_day_passes(self, staff_assigned):
        await _set_availability(staff_assigned, [(1, "full", None, None)])
        assert (await create(staff_assigned, blocks=[block("00:00", "23:55", [1])])).group_id

    async def test_one_time_schedule_ignores_availability(self, async_client, admin_headers, staff_assigned):
        """④ 는 패턴 저장에만 — 일반 스케줄은 off 요일에도 저장된다."""
        day = next_dow(FAR, 3)
        await _set_availability(staff_assigned, [(3, "off", None, None)])
        resp = await async_client.post("/api/v1/console/schedules", headers=admin_headers, json={
            "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
            "work_date": day.isoformat(), "start_time": "09:00", "end_time": "17:00", "force": True,
        })
        assert resp.status_code == 201, resp.text


# ─── update ─────────────────────────────────────────────────────


class TestUpdate:
    async def test_not_started_group_is_replaced_in_place(self, staff_assigned):
        out = await create(staff_assigned, blocks=[block("09:00", "17:00", [1, 3])])
        async with admin_session(staff_assigned) as (db, admin):
            new = await svc.update_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                actor=admin, data=group_in(staff_assigned, blocks=[block("10:00", "18:00", [2])]),
            )
        assert new.group_id == out.group_id
        rows = await rows_of(out.group_id)
        assert len(rows) == 1 and rows[0].byday == [2] and rows[0].start_date == FAR

    async def test_started_group_ends_old_rows_yesterday_and_starts_new_today(self, staff_assigned):
        out = await create(staff_assigned, start_date=TODAY - timedelta(days=14),
                           blocks=[block("09:00", "17:00", byday=ALL_DAYS)])
        before = await schedules_of(staff_assigned)
        assert len(before) == 15
        async with admin_session(staff_assigned) as (db, admin):
            await svc.update_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                actor=admin,
                data=group_in(staff_assigned, start_date=TODAY - timedelta(days=14),
                              blocks=[block("10:00", "18:00", byday=ALL_DAYS)]),
            )
        rows = await rows_of(out.group_id)
        assert len(rows) == 2
        old, new = rows
        assert old.until_date == TODAY - timedelta(days=1) and old.start_time.hour == 9
        assert new.start_date == TODAY and new.until_date is None and new.start_time.hour == 10
        # 창 안 실 행은 새 값으로 sweep 되고 새 pattern 으로 도장이 옮겨진다 (overridden 은 안 켬)
        after = await schedules_of(staff_assigned, include_deleted=False)
        assert len(after) == 15
        assert all(r.start_at.hour == 10 and r.pattern_id == new.id and not r.pattern_overridden for r in after)

    async def test_started_group_removing_a_day_deletes_future_auto_rows(self, staff_assigned):
        out = await create(staff_assigned, start_date=TODAY - timedelta(days=7),
                           blocks=[block(byday=ALL_DAYS)])
        async with admin_session(staff_assigned) as (db, admin):
            await svc.update_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                actor=admin,
                data=group_in(staff_assigned, start_date=TODAY - timedelta(days=7), blocks=[block(byday=[1])]),
            )
        live = await schedules_of(staff_assigned, include_deleted=False)
        assert live and all(dow_sun0(r.operating_day) == 1 for r in live)

    async def test_update_overlapping_other_group_is_409(self, staff_assigned):
        a = await create(staff_assigned, blocks=[block(byday=[1])])
        await create(staff_assigned, blocks=[block(byday=[2])])
        with pytest.raises(AppError) as ei:
            async with admin_session(staff_assigned) as (db, admin):
                await svc.update_group(
                    db, organization_id=staff_assigned["organization_id"], group_id=UUID(a.group_id),
                    actor=admin, data=group_in(staff_assigned, blocks=[block(byday=[1, 2])]),
                )
        assert code_of(ei.value) == "PATTERN_OVERLAP_EXISTING"


# ─── move ───────────────────────────────────────────────────────


class TestMove:
    async def test_delta_shifts_start_and_until(self, staff_assigned):
        out = await create(staff_assigned, until_date=FAR + timedelta(days=30))
        async with admin_session(staff_assigned) as (db, admin):
            moved = await svc.move_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                actor=admin, delta_days=-7,
            )
        assert moved.start_date == FAR - timedelta(days=7)
        assert moved.until_date == FAR + timedelta(days=23)

    async def test_moving_into_window_materializes(self, staff_assigned):
        out = await create(staff_assigned, blocks=[block(byday=ALL_DAYS)])
        async with admin_session(staff_assigned) as (db, admin):
            await svc.move_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                actor=admin, delta_days=-(FAR - TODAY).days,
            )
        assert len(await schedules_of(staff_assigned)) == 15

    async def test_into_past_is_409(self, staff_assigned):
        out = await create(staff_assigned)
        with pytest.raises(AppError) as ei:
            async with admin_session(staff_assigned) as (db, admin):
                await svc.move_group(
                    db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                    actor=admin, delta_days=-((FAR - TODAY).days + 1),
                )
        assert ei.value.status_code == 409
        assert code_of(ei.value) == "PATTERN_MOVE_INTO_PAST"

    async def test_started_group_is_409(self, staff_assigned):
        out = await create(staff_assigned, start_date=TODAY)
        with pytest.raises(AppError) as ei:
            async with admin_session(staff_assigned) as (db, admin):
                await svc.move_group(
                    db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id),
                    actor=admin, delta_days=3,
                )
        assert code_of(ei.value) == "PATTERN_GROUP_STARTED"


# ─── delete ─────────────────────────────────────────────────────


class TestDelete:
    async def test_past_rows_become_one_time_future_auto_rows_removed(self, staff_assigned):
        out = await create(staff_assigned, start_date=TODAY - timedelta(days=3),
                           blocks=[block(byday=ALL_DAYS)])
        # 과거 3일치는 실체화 창 밖이라 직접 실체화해 둔다 (materialize_window 로 — 서비스 경유)
        from app.services.fixed_schedule.materialize import materialize_window
        async with async_session() as db:
            n = await materialize_window(
                db, organization_id=staff_assigned["organization_id"], user_ids=[staff_assigned["id"]],
                date_from=TODAY - timedelta(days=3), date_to=TODAY - timedelta(days=1),
            )
        assert n == 3
        async with admin_session(staff_assigned) as (db, admin):
            await svc.delete_group(
                db, organization_id=staff_assigned["organization_id"], group_id=UUID(out.group_id), actor=admin,
            )
        assert await rows_of(out.group_id) == []
        rows = await schedules_of(staff_assigned)
        past = [r for r in rows if r.operating_day < TODAY]
        future = [r for r in rows if r.operating_day >= TODAY]
        assert len(past) == 3 and all(r.status == "confirmed" and r.pattern_id is None for r in past)
        assert future and all(r.status == "deleted" for r in future)

    async def test_unknown_group_is_404(self, staff_assigned):
        from uuid import uuid4
        with pytest.raises(AppError) as ei:
            async with admin_session(staff_assigned) as (db, admin):
                await svc.delete_group(db, organization_id=staff_assigned["organization_id"], group_id=uuid4(), actor=admin)
        assert ei.value.status_code == 404 and ei.value.detail["code"] == "PATTERN_NOT_FOUND"


class TestList:
    async def test_list_hides_ended_groups_unless_asked(self, staff_assigned):
        ended_until = TODAY - timedelta(days=1)
        await create(staff_assigned, start_date=TODAY - timedelta(days=30), until_date=ended_until,
                     blocks=[block(byday=[0])])
        live = await create(staff_assigned)
        async with async_session() as db:
            default = await svc.list_for_user(db, organization_id=staff_assigned["organization_id"], user_id=staff_assigned["id"])
            everything = await svc.list_for_user(
                db, organization_id=staff_assigned["organization_id"], user_id=staff_assigned["id"], include_ended=True,
            )
        assert [g.group_id for g in default] == [live.group_id]
        assert len(everything) == 2
