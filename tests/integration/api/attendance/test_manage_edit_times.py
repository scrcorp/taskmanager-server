"""Integration tests — POST /api/v1/attendance/manage/attendance/times.

"상태는 그대로 두고 시각만 고친다" 계약을 고정한다. 이 엔드포인트가 생긴 이유는
clock-in 시각 하나를 3분 당기려고 Undo Clock-in → 재입력을 하면서 break 기록과
anomaly 가 함께 날아가던 문제(2026-08-09 제보)라서, **status/anomalies 불변**이
가장 중요한 assert 다.

커버:
  - clock_in 만 보정 → status/anomalies 불변, timeline 1행
  - clock_out 보정 + total_work_minutes 재계산
  - break 세션 시각 보정 → duration/total_break_minutes 재계산, timeline break 3행
  - 순서/범위/겹침 위반 400
  - 진행 중 break 의 end 편집 400
  - 찍히지 않은 clock_in/out 편집 400, 없는 break 404, 빈 요청 400
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.attendance_break import AttendanceBreak
from app.models.schedule import Schedule
from app.models.user_store import UserStore
from app.services import attendance_timeline as tl


pytestmark = pytest.mark.asyncio

ENDPOINT = "/api/v1/attendance/manage/attendance/times"


# ── fixtures / helpers ────────────────────────────────────────────────


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    async with async_session() as db:
        existing = (await db.execute(
            select(UserStore).where(
                UserStore.user_id == user_id,
                UserStore.store_id == store_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager))
        elif is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()


@pytest_asyncio.fixture
async def manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    test_users: dict,
) -> dict:
    """testgm 으로 manage session 을 연 kiosk 헤더."""
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def _seed(
    user_info: dict,
    store_id: UUID,
    *,
    status: str = "working",
    clock_in_hhmm: time | None = time(9, 0),
    clock_out_hhmm: time | None = None,
    breaks: list[tuple[time, time | None, str]] | None = None,
    anomalies: list[str] | None = None,
) -> tuple[UUID, date]:
    """오늘 confirmed 스케줄 + attendance(+breaks) 시드. Returns (attendance_id, work_date).

    시각 인자는 **매장 벽시계** — 저장은 store tz instant 로 합성한다.
    """
    from zoneinfo import ZoneInfo
    from app.utils.timezone import get_store_day_config, get_work_date

    async with async_session() as db:
        tz_name, day_cfg = await get_store_day_config(db, store_id)
        tz = ZoneInfo(tz_name)
        today = get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))

        def _at(t: time) -> datetime:
            return datetime.combine(today, t, tzinfo=tz)

        sched = Schedule(
            organization_id=user_info["organization_id"],
            user_id=user_info["id"],
            store_id=store_id,
            operating_day=today,
            start_at=datetime.combine(today, time(9, 0)),  # 벽시계 naive 계약
            end_at=datetime.combine(today, time(17, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=user_info["organization_id"],
            store_id=store_id,
            user_id=user_info["id"],
            schedule_id=sched.id,
            work_date=today,
            status=status,
            anomalies=anomalies,
            clock_in=_at(clock_in_hhmm) if clock_in_hhmm else None,
            clock_in_timezone=tz_name if clock_in_hhmm else None,
            clock_out=_at(clock_out_hhmm) if clock_out_hhmm else None,
            clock_out_timezone=tz_name if clock_out_hhmm else None,
        )
        db.add(att)
        await db.flush()
        for start_t, end_t, btype in (breaks or []):
            started = _at(start_t)
            ended = _at(end_t) if end_t else None
            db.add(AttendanceBreak(
                attendance_id=att.id,
                started_at=started,
                ended_at=ended,
                break_type=btype,
                duration_minutes=(
                    None if ended is None
                    else int((ended - started).total_seconds() // 60)
                ),
            ))
        await db.commit()
        return att.id, today


async def _fetch(attendance_id: UUID) -> Attendance:
    async with async_session() as db:
        return (await db.execute(
            select(Attendance).where(Attendance.id == attendance_id)
        )).scalar_one()


async def _fetch_breaks(attendance_id: UUID) -> list[AttendanceBreak]:
    async with async_session() as db:
        rows = (await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id == attendance_id)
            .order_by(AttendanceBreak.started_at)
        )).scalars().all()
        return list(rows)


async def _fetch_corrections(attendance_id: UUID) -> list[AttendanceCorrection]:
    async with async_session() as db:
        rows = (await db.execute(
            select(AttendanceCorrection)
            .where(AttendanceCorrection.attendance_id == attendance_id)
        )).scalars().all()
        return list(rows)


async def _cleanup(attendance_id: UUID) -> None:
    async with async_session() as db:
        await db.execute(
            delete(AttendanceCorrection).where(
                AttendanceCorrection.attendance_id == attendance_id
            )
        )
        await db.execute(
            delete(AttendanceBreak).where(AttendanceBreak.attendance_id == attendance_id)
        )
        await db.execute(delete(Attendance).where(Attendance.id == attendance_id))
        await db.commit()


def _local_hhmm(value: datetime, tz_name: str) -> str:
    from zoneinfo import ZoneInfo
    return value.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


# ── happy path ────────────────────────────────────────────────────────


async def test_edit_clock_in_only_keeps_status_and_anomalies(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """clock_in 만 보정 — status/anomalies 는 손대지 않는다 (이 엔드포인트의 존재 이유)."""
    att_id, _today = await _seed(
        test_user, test_store_id, status="working", anomalies=["late"]
    )
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "clock_in_hhmm": "08:57",
                "reason": "Wrong time recorded",
            },
        )
        assert resp.status_code == 200, resp.text

        att = await _fetch(att_id)
        assert att.status == "working"
        assert att.anomalies == ["late"]
        assert _local_hhmm(att.clock_in, att.clock_in_timezone) == "08:57"
        assert att.clock_out is None

        rows = await _fetch_corrections(att_id)
        assert [r.field_name for r in rows] == [tl.FIELD_CLOCK_IN]
        assert rows[0].action == tl.ACTION_MODIFY
        assert rows[0].reason == "Wrong time recorded"
        assert rows[0].original_value != tl.NONE  # before 는 항상 채워진다
    finally:
        await _cleanup(att_id)


async def test_edit_accepts_one_minute_granularity(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """5분 배수가 아닌 시각도 그대로 저장 — clock 시각엔 그리드 제약이 없다."""
    att_id, _today = await _seed(
        test_user, test_store_id, status="clocked_out", clock_out_hhmm=time(17, 0)
    )
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "clock_in_hhmm": "09:03",
                "clock_out_hhmm": "17:11",
                "reason": "exact times",
            },
        )
        assert resp.status_code == 200, resp.text

        att = await _fetch(att_id)
        assert _local_hhmm(att.clock_in, att.clock_in_timezone) == "09:03"
        assert _local_hhmm(att.clock_out, att.clock_out_timezone) == "17:11"
        # 09:03 → 17:11 = 488분
        assert att.total_work_minutes == 488
    finally:
        await _cleanup(att_id)


async def test_edit_break_times_recalculates_totals(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """break 세션 시각 보정 → duration + total_break_minutes 재계산, 이력 3행."""
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="clocked_out",
        clock_out_hhmm=time(17, 0),
        breaks=[(time(12, 0), time(12, 30), "unpaid_meal")],
    )
    try:
        breaks = await _fetch_breaks(att_id)
        assert len(breaks) == 1
        target_break = breaks[0]

        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "Break correction",
                "breaks": [{
                    "break_id": str(target_break.id),
                    "start_hhmm": "12:07",
                    "end_hhmm": "12:44",
                }],
            },
        )
        assert resp.status_code == 200, resp.text

        after = (await _fetch_breaks(att_id))[0]
        assert after.duration_minutes == 37
        att = await _fetch(att_id)
        assert att.total_break_minutes == 37
        assert att.status == "clocked_out"  # 상태 불변

        rows = await _fetch_corrections(att_id)
        # break 스냅샷은 (start, end, type) 3행이지만 type 은 안 바뀌었으므로 2행만 남는다
        fields = sorted(r.field_name for r in rows)
        assert fields == [tl.FIELD_BREAK_END_AT, tl.FIELD_BREAK_START_AT]
        assert {r.target_id for r in rows} == {target_break.id}
        assert {r.group_id for r in rows} == {rows[0].group_id}  # 한 편집 = 한 그룹
    finally:
        await _cleanup(att_id)


async def test_clock_and_break_edits_share_one_group(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """clock 과 break 를 한 번에 고치면 이력이 한 카드로 묶인다."""
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="clocked_out",
        clock_out_hhmm=time(17, 0),
        breaks=[(time(12, 0), time(12, 30), "paid_10min")],
    )
    try:
        target_break = (await _fetch_breaks(att_id))[0]
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "full correction",
                "clock_in_hhmm": "08:58",
                "breaks": [{"break_id": str(target_break.id), "start_hhmm": "12:02"}],
            },
        )
        assert resp.status_code == 200, resp.text

        rows = await _fetch_corrections(att_id)
        assert len({r.group_id for r in rows}) == 1
        assert tl.FIELD_CLOCK_IN in {r.field_name for r in rows}
        assert tl.FIELD_BREAK_START_AT in {r.field_name for r in rows}
    finally:
        await _cleanup(att_id)


async def test_open_break_start_can_be_edited(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """진행 중 break 도 **시작 시각**은 고칠 수 있다 (종료만 막는다)."""
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="on_break",
        breaks=[(time(12, 0), None, "paid_10min")],
    )
    try:
        target_break = (await _fetch_breaks(att_id))[0]
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "started earlier",
                "breaks": [{"break_id": str(target_break.id), "start_hhmm": "11:52"}],
            },
        )
        assert resp.status_code == 200, resp.text

        after = (await _fetch_breaks(att_id))[0]
        assert after.ended_at is None
        assert after.duration_minutes is None
        att = await _fetch(att_id)
        assert att.status == "on_break"
        assert att.total_break_minutes == 0  # 열린 세션은 합계에 안 들어간다
    finally:
        await _cleanup(att_id)


# ── 거부 경로 ─────────────────────────────────────────────────────────


async def test_empty_request_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(test_user, test_store_id)
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={"user_id": str(test_user["id"]), "reason": "nothing"},
        )
        assert resp.status_code == 400
        assert "Nothing to change" in resp.text
    finally:
        await _cleanup(att_id)


async def test_clock_out_before_clock_in_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(
        test_user, test_store_id, status="clocked_out", clock_out_hhmm=time(17, 0)
    )
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "bad order",
                "clock_out_hhmm": "08:30",
            },
        )
        assert resp.status_code == 400
        assert "after clock-in" in resp.text

        att = await _fetch(att_id)  # 아무것도 안 바뀌어야 한다
        assert _local_hhmm(att.clock_out, att.clock_out_timezone) == "17:00"
    finally:
        await _cleanup(att_id)


async def test_edit_clock_in_without_clock_in_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """출근 기록이 없는데 시각만 고치라는 건 사고 — Clock In 액션으로 안내."""
    att_id, _today = await _seed(
        test_user, test_store_id, status="upcoming", clock_in_hhmm=None
    )
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "no clock-in yet",
                "clock_in_hhmm": "09:00",
            },
        )
        assert resp.status_code == 400
        assert "Clock In action" in resp.text
    finally:
        await _cleanup(att_id)


async def test_edit_open_break_end_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="on_break",
        breaks=[(time(12, 0), None, "paid_10min")],
    )
    try:
        target_break = (await _fetch_breaks(att_id))[0]
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "close it",
                "breaks": [{"break_id": str(target_break.id), "end_hhmm": "12:10"}],
            },
        )
        assert resp.status_code == 400
        assert "still in progress" in resp.text
    finally:
        await _cleanup(att_id)


async def test_break_outside_clock_window_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="clocked_out",
        clock_out_hhmm=time(17, 0),
        breaks=[(time(12, 0), time(12, 30), "unpaid_meal")],
    )
    try:
        target_break = (await _fetch_breaks(att_id))[0]
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "before clock-in",
                "breaks": [{
                    "break_id": str(target_break.id),
                    "start_hhmm": "08:30",
                    "end_hhmm": "08:50",
                }],
            },
        )
        assert resp.status_code == 400
        assert "before clock-in" in resp.text
    finally:
        await _cleanup(att_id)


async def test_overlapping_breaks_rejected(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="clocked_out",
        clock_out_hhmm=time(17, 0),
        breaks=[
            (time(12, 0), time(12, 30), "unpaid_meal"),
            (time(15, 0), time(15, 10), "paid_10min"),
        ],
    )
    try:
        second = (await _fetch_breaks(att_id))[1]
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "overlap",
                "breaks": [{
                    "break_id": str(second.id),
                    "start_hhmm": "12:20",
                    "end_hhmm": "12:40",
                }],
            },
        )
        assert resp.status_code == 400
        assert "overlap" in resp.text.lower()

        # 저장은 롤백 — 원래 시각 유지
        rows = await _fetch_breaks(att_id)
        assert _local_hhmm(rows[1].started_at, "UTC") is not None
        assert rows[1].duration_minutes == 10
    finally:
        await _cleanup(att_id)


async def test_unknown_break_id_returns_404(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    att_id, _today = await _seed(test_user, test_store_id)
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=manage_headers,
            json={
                "user_id": str(test_user["id"]),
                "reason": "ghost break",
                "breaks": [{"break_id": str(uuid4()), "start_hhmm": "12:00"}],
            },
        )
        assert resp.status_code == 404
    finally:
        await _cleanup(att_id)


async def test_requires_manage_session(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """manage 세션 없이 device 토큰만으로는 호출 불가."""
    att_id, _today = await _seed(test_user, test_store_id)
    try:
        resp = await async_client.post(
            ENDPOINT,
            headers=device_auth_headers,
            json={"user_id": str(test_user["id"]), "clock_in_hhmm": "09:00"},
        )
        assert resp.status_code in (401, 403, 422)
    finally:
        await _cleanup(att_id)


async def test_break_id_exposed_in_manage_schedules(
    async_client: AsyncClient,
    manage_headers: dict,
    test_store_id: UUID,
    test_user: dict,
) -> None:
    """Edit Times 가 break 를 지목하려면 목록 응답에 break_id 가 있어야 한다."""
    att_id, _today = await _seed(
        test_user,
        test_store_id,
        status="clocked_out",
        clock_out_hhmm=time(17, 0),
        breaks=[(time(12, 0), time(12, 30), "unpaid_meal")],
    )
    try:
        resp = await async_client.get(
            "/api/v1/attendance/manage/schedules", headers=manage_headers
        )
        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json() if r["user_id"] == str(test_user["id"])]
        assert rows, "seeded staff missing from manage/schedules"
        breaks = rows[0]["breaks"]
        assert breaks and UUID(breaks[0]["break_id"])
    finally:
        await _cleanup(att_id)


# 영업일 경계(자정 넘김) 는 status 엔드포인트와 같은 `_combine_business_day` 를 쓰므로
# test_manage_tz_correction.py / test_early_morning_lifecycle.py 가 이미 고정하고 있다.
