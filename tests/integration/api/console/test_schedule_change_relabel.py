"""API integration — 스케줄 시각을 고치면 이미 출근한 근태의 라벨도 다시 매겨진다 (D11).

원인 B: `recompute_attendance_for_schedule_change` 가 clock_in 이 있는 row 를 통째로
건너뛰어서, 17시 스케줄을 14시로 고쳐도 15시 출근이 계속 "조기 출근" 으로 남았다.
보존해야 하는 건 *시각* 이지 *라벨* 이 아니다.

여기서 지키는 것:
    - PATCH /console/schedules/{id} 로 시각을 바꾸면 status/anomalies 가 새 스케줄
      기준으로 다시 매겨진다 (early ↔ late ↔ 정시)
    - clock_in / clock_out 값은 절대 바뀌지 않는다
    - 전/후가 attendance timeline(= attendance_corrections)에 남는다
    - early 라벨이 걷히거나 붙으면 매니저 확인 도장(early_clock_in_confirmed_*)이
      리셋된다 — 남겨두면 payroll 게이트 ⑥ 가 "확인됨" 으로 조용히 통과한다
    - 판정이 소유하지 않는 라벨(auto_clocked_out)은 사라지지 않는다
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.schedule import Schedule
from app.models.user_store import UserStore
from app.services import attendance_timeline as tl
from app.services.attendance_service import ANOMALY_EARLY_CLOCK_IN_OVERRIDE

pytestmark = pytest.mark.asyncio

SCHED_URL = "/api/v1/console/schedules"

# 이 모듈이 만든 근무 배정 — 남기면 "이 유저는 이 매장 소속이 아니다" 를 전제로 하는
# 다른 테스트가 조용히 통과한다. 만든 것은 되돌린다.
_created_assignments: list[tuple[UUID, UUID]] = []


@pytest.fixture(autouse=True)
async def _cleanup_assignments():
    yield
    if not _created_assignments:
        return
    async with async_session() as db:
        for user_id, store_id in _created_assignments:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id == store_id,
                )
            )
        await db.commit()
    _created_assignments.clear()


def _day() -> date:
    """확정된 급여 기간과 겹치지 않도록 가까운 미래를 쓴다."""
    return date.today() + timedelta(days=3)


async def _setup(
    staff: dict,
    store_id: UUID,
    *,
    sched_start: time,
    sched_end: time,
    clock_in: time | None,
    clock_out: time | None = None,
    status: str,
    anomalies: list[str] | None,
    total_work_minutes: int | None = None,
    early_confirmed_by: UUID | None = None,
) -> tuple[UUID, UUID]:
    """스케줄 1건 + 그에 묶인 attendance 1건. (schedule_id, attendance_id) 반환.

    테스트 매장은 timezone=UTC 라 벽시계 == UTC 절대시각이다.
    """
    day = _day()
    async with async_session() as db:
        assigned = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == staff["id"],
                UserStore.store_id == store_id,
            )
        )
        if assigned is None:
            db.add(
                UserStore(
                    user_id=staff["id"],
                    store_id=store_id,
                    is_work_assignment=True,
                )
            )
            _created_assignments.append((staff["id"], store_id))
        elif not assigned.is_work_assignment:
            assigned.is_work_assignment = True
        await db.flush()

        sched = Schedule(
            organization_id=staff["organization_id"],
            user_id=staff["id"],
            store_id=store_id,
            operating_day=day,
            start_at=datetime.combine(day, sched_start),
            end_at=datetime.combine(day, sched_end),
            status="confirmed",
        )
        db.add(sched)
        await db.flush()

        att = Attendance(
            organization_id=staff["organization_id"],
            store_id=store_id,
            user_id=staff["id"],
            schedule_id=sched.id,
            work_date=day,
            clock_in=(
                datetime.combine(day, clock_in, tzinfo=timezone.utc)
                if clock_in
                else None
            ),
            clock_in_timezone="UTC" if clock_in else None,
            clock_out=(
                datetime.combine(day, clock_out, tzinfo=timezone.utc)
                if clock_out
                else None
            ),
            clock_out_timezone="UTC" if clock_out else None,
            status=status,
            anomalies=anomalies,
            total_work_minutes=total_work_minutes,
            early_clock_in_confirmed_by=early_confirmed_by,
            early_clock_in_confirmed_at=(
                datetime.now(timezone.utc) if early_confirmed_by else None
            ),
        )
        db.add(att)
        await db.commit()
        return sched.id, att.id


async def _fetch(att_id: UUID) -> Attendance:
    async with async_session() as db:
        return (
            await db.execute(select(Attendance).where(Attendance.id == att_id))
        ).scalar_one()


async def _corrections(att_id: UUID) -> list[AttendanceCorrection]:
    async with async_session() as db:
        return list(
            (
                await db.execute(
                    select(AttendanceCorrection)
                    .where(AttendanceCorrection.attendance_id == att_id)
                    .order_by(AttendanceCorrection.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _move_start(async_client, headers, schedule_id: UUID, start: time, end: time):
    day = _day()
    return await async_client.patch(
        f"{SCHED_URL}/{schedule_id}",
        headers=headers,
        json={
            "start_at": f"{day.isoformat()}T{start.strftime('%H:%M')}",
            "end_at": f"{day.isoformat()}T{end.strftime('%H:%M')}",
            "force": True,
        },
    )


# ── (a) early → late 로 뒤집히고 이력이 남는다 ─────────────────────────────


async def test_early_clock_in_becomes_late_when_shift_moved_earlier(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """17시 스케줄에 15시 출근(early) → 스케줄 14시로 변경 → late + 이력."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(15, 0),
        status="working",
        anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE],
    )

    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(14, 0), time(21, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.status == "late"
    assert att.anomalies == ["late"]

    rows = await _corrections(att_id)
    status_rows = [r for r in rows if r.field_name == tl.FIELD_STATUS]
    label_rows = [r for r in rows if r.field_name == tl.FIELD_ANOMALIES]
    assert status_rows, "status 전이가 이력에 남아야 한다"
    assert status_rows[-1].original_value == "working"
    assert status_rows[-1].corrected_value == "late"
    assert label_rows, "라벨 전이가 이력에 남아야 한다"
    assert label_rows[-1].original_value == ANOMALY_EARLY_CLOCK_IN_OVERRIDE
    assert label_rows[-1].corrected_value == "late"
    # 콘솔이 모르는 action 은 raw 로 그려지므로 기존 상수를 재사용한다.
    assert label_rows[-1].action == tl.ACTION_MODIFY
    assert label_rows[-1].reason == "Shift time changed"
    # 한 번의 스케줄 수정 = 카드 한 장
    assert status_rows[-1].group_id == label_rows[-1].group_id
    assert label_rows[-1].corrected_by == test_users["testadmin"]["id"]


# ── (b) late → 정시 ───────────────────────────────────────────────────────


async def test_late_becomes_on_time_when_shift_moved_later(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """지각으로 기록된 건이 스케줄을 늦추면 정시가 된다."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(9, 0),
        sched_end=time(17, 0),
        clock_in=time(11, 0),
        status="late",
        anomalies=["late"],
    )

    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(11, 0), time(19, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.status == "working"
    assert att.anomalies is None

    label_rows = [
        r for r in await _corrections(att_id) if r.field_name == tl.FIELD_ANOMALIES
    ]
    assert label_rows[-1].original_value == "late"
    assert label_rows[-1].corrected_value == tl.NONE


# ── (c) 시각은 절대 안 바뀐다 ─────────────────────────────────────────────


async def test_clock_times_are_never_touched(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """라벨만 다시 매긴다 — clock_in/clock_out 은 실제로 일어난 사건이다."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(15, 0),
        clock_out=time(21, 30),
        status="clocked_out",
        anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE],
        total_work_minutes=390,
    )
    before = await _fetch(att_id)
    before_in, before_out, before_minutes = (
        before.clock_in,
        before.clock_out,
        before.total_work_minutes,
    )

    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(14, 0), time(18, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.clock_in == before_in
    assert att.clock_out == before_out
    assert att.total_work_minutes == before_minutes
    # 퇴근한 row 의 status 는 사건이 정한다 — 재판정이 바꾸지 않는다.
    assert att.status == "clocked_out"
    # 라벨은 새 스케줄 기준: 15시 출근은 14시 시작 대비 지각,
    # 예정 4시간(14~18) 대비 390분 근무는 초과근무.
    assert set(att.anomalies or []) == {"late", "overtime"}


# ── (d) 매니저 확인 도장 처리 ─────────────────────────────────────────────


async def test_early_confirmation_is_reset_when_label_is_removed(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """early 라벨이 걷히면 확인 도장도 함께 지운다.

    남겨두면 스케줄이 되돌려져 라벨이 다시 붙었을 때 **아무도 확인한 적 없는 건이**
    payroll 게이트 ⑥(anomaly + confirmed_at IS NULL)을 "확인됨" 으로 통과한다.
    사유 문자열 자체는 append-only 인 attendance_corrections 에 남아 사라지지 않는다.
    """
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(15, 0),
        status="working",
        anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE],
        early_confirmed_by=test_users["testgm"]["id"],
    )
    assert (await _fetch(att_id)).early_clock_in_confirmed_at is not None

    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(15, 0), time(21, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.anomalies is None
    assert att.early_clock_in_confirmed_at is None
    assert att.early_clock_in_confirmed_by is None


async def test_early_confirmation_survives_when_label_unchanged(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """라벨 유무가 그대로면 확인 도장은 보존된다 — 무의미한 재확인 요구를 만들지 않는다."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(15, 0),
        status="working",
        anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE],
        early_confirmed_by=test_users["testgm"]["id"],
    )

    # 종료만 늘린다 → 여전히 조기 출근
    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(17, 0), time(22, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.anomalies == [ANOMALY_EARLY_CLOCK_IN_OVERRIDE]
    assert att.early_clock_in_confirmed_at is not None
    assert att.early_clock_in_confirmed_by == test_users["testgm"]["id"]
    # 라벨도 status 도 그대로 → 이력 소음이 남지 않는다
    assert not await _corrections(att_id)


# ── 보존 라벨 ─────────────────────────────────────────────────────────────


async def test_unowned_anomaly_survives_relabel(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """auto_clocked_out 은 재판정이 손대지 않는다 — 사라지면 payroll 게이트 ① 이 통과한다."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(15, 0),
        clock_out=time(21, 0),
        status="clocked_out",
        anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE, "auto_clocked_out"],
        total_work_minutes=360,
    )

    resp = await _move_start(
        async_client, admin_headers, schedule_id, time(14, 0), time(21, 0)
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert "auto_clocked_out" in (att.anomalies or [])
    assert "late" in (att.anomalies or [])
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE not in (att.anomalies or [])


# ── 미출근 row 회귀 ───────────────────────────────────────────────────────


async def test_upcoming_row_still_syncs_work_date(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """clock_in 없는 row 는 기존대로 영업일까지 스케줄을 따라간다 (회귀 방지)."""
    staff = test_users["teststaff"]
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=None,
        status="upcoming",
        anomalies=None,
    )
    new_day = _day() + timedelta(days=1)

    resp = await async_client.patch(
        f"{SCHED_URL}/{schedule_id}",
        headers=admin_headers,
        json={
            "operating_day": new_day.isoformat(),
            "start_at": f"{new_day.isoformat()}T17:00",
            "end_at": f"{new_day.isoformat()}T21:00",
            "force": True,
        },
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.work_date == new_day
    assert att.status == "upcoming"


async def test_recorded_row_keeps_its_work_date(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """기록이 있는 row 의 work_date 는 옮기지 않는다.

    work_date 는 payroll 기간 귀속 키(모든 게이트 쿼리가 `work_date BETWEEN`)이고,
    실제로 일이 일어난 영업일이다. 라벨 재판정이 급여 귀속일을 조용히 옮기면 안 된다.
    """
    staff = test_users["teststaff"]
    original_day = _day()
    schedule_id, att_id = await _setup(
        staff,
        test_store_id,
        sched_start=time(17, 0),
        sched_end=time(21, 0),
        clock_in=time(17, 0),
        status="working",
        anomalies=None,
    )
    new_day = original_day + timedelta(days=1)

    resp = await async_client.patch(
        f"{SCHED_URL}/{schedule_id}",
        headers=admin_headers,
        json={
            "operating_day": new_day.isoformat(),
            "start_at": f"{new_day.isoformat()}T17:00",
            "end_at": f"{new_day.isoformat()}T21:00",
            "force": True,
        },
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.work_date == original_day
