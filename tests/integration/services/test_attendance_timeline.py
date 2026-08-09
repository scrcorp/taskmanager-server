"""Integration tests — Activity History 타임라인 계약.

검증하는 계약 (app/services/attendance_timeline.py):
  1. **before 는 절대 비지 않는다** — 값이 없던 자리는 NULL 이 아니라 센티널
     `(none)` / `(empty)` 로 남는다. 이게 콘솔 Before 가 전부 `—` 로 보이던
     원인이었으므로 회귀 방지의 핵심.
  2. 한 사용자 액션 = 한 group_id, 그 안에 항목마다 한 행.
  3. status 전이는 모든 액션에서 함께 남는다 (clock-in 이전 = upcoming).
  4. **break 세션 추가/수정/삭제도 이력을 남긴다** — 예전엔 아무것도 안 남았다.
  5. 그룹의 사유를 고치면 같은 그룹 행 전체에 반영된다 (카드 안에서 사유가 갈리지 않게).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.attendance_break import BREAK_TYPE_PAID_10MIN, BREAK_TYPE_UNPAID_MEAL
from app.models.schedule import Schedule
from app.services import attendance_timeline as tl
from app.services.attendance_action_service import attendance_action_service
from app.services.attendance_service import attendance_service

pytestmark = pytest.mark.asyncio


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def _make_attendance(
    test_user: dict,
    store_id: UUID,
    *,
    status: str = "upcoming",
    clock_in: datetime | None = None,
) -> UUID:
    """오늘 09:00–17:00 스케줄 + attendance 한 건."""
    day = _today()
    async with async_session() as db:
        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=store_id,
            operating_day=day,
            start_at=datetime.combine(day, time(9, 0)),
            end_at=datetime.combine(day, time(17, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.flush()
        att = Attendance(
            organization_id=test_user["organization_id"],
            store_id=store_id,
            user_id=test_user["id"],
            schedule_id=sched.id,
            work_date=day,
            clock_in=clock_in,
            clock_in_timezone="UTC" if clock_in else None,
            status=status,
        )
        db.add(att)
        await db.commit()
        return att.id


async def _rows(att_id: UUID) -> list[AttendanceCorrection]:
    async with async_session() as db:
        result = await db.execute(
            select(AttendanceCorrection)
            .where(AttendanceCorrection.attendance_id == att_id)
            .order_by(AttendanceCorrection.created_at)
        )
        return list(result.scalars().all())


def _by_field(rows: list[AttendanceCorrection]) -> dict[str, AttendanceCorrection]:
    return {r.field_name: r for r in rows}


# ── 1. clock-in — 상태 전이 + 시각 전이, before 모두 채워짐 ────────────────


async def test_clock_in_records_status_and_time_with_before(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """출근은 upcoming → working 상태 전이와 Not set → 시각 전이를 함께 남긴다."""
    att_id = await _make_attendance(test_user, test_store_id)
    at = datetime.combine(_today(), time(9, 2), tzinfo=timezone.utc)

    async with async_session() as db:
        await attendance_action_service.clock_in(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            at=at,
            reason="Manual clock-in",
            by_user_id=test_user["id"],
        )

    rows = await _rows(att_id)
    assert len(rows) == 2
    # 한 액션 = 한 그룹
    assert len({r.group_id for r in rows}) == 1
    assert {r.action for r in rows} == {"clock_in"}
    # 핵심 회귀 가드 — before 가 비어 있는 행이 하나도 없어야 한다
    assert all(r.original_value for r in rows)

    fields = _by_field(rows)
    assert fields["status"].original_value == "upcoming"
    assert fields["status"].corrected_value in ("working", "late")
    assert fields["clock_in"].original_value == tl.NONE
    assert fields["clock_in"].corrected_value.startswith(str(_today()))


# ── 2. break 시작/종료 — 세션을 target 으로 지목 ──────────────────────────


async def test_break_start_and_end_record_session_transitions(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """휴식 시작/종료가 어느 세션의 무엇을 바꿨는지 행으로 남는다."""
    clock_in = datetime.combine(_today(), time(9, 0), tzinfo=timezone.utc)
    att_id = await _make_attendance(
        test_user, test_store_id, status="working", clock_in=clock_in
    )

    async with async_session() as db:
        await attendance_action_service.start_break(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            at=clock_in + timedelta(hours=2),
            break_type=BREAK_TYPE_UNPAID_MEAL,
            reason="Lunch",
            by_user_id=test_user["id"],
        )

    rows = await _rows(att_id)
    fields = _by_field(rows)
    assert fields["status"].original_value == "working"
    assert fields["status"].corrected_value == "on_break"
    # 세션 생성이므로 시작시각/타입은 "없음 → 값" 전이
    assert fields["break_start_at"].original_value == tl.NONE
    assert fields["break_type"].original_value == tl.NONE
    assert fields["break_type"].corrected_value == BREAK_TYPE_UNPAID_MEAL
    # 휴식 행은 세션을 지목한다
    assert fields["break_start_at"].target_type == "break"
    assert fields["break_start_at"].target_id is not None
    # 아직 안 끝났으므로 종료시각 행은 없다 (before == after 인 행은 안 남김)
    assert "break_end_at" not in fields

    async with async_session() as db:
        await attendance_action_service.end_break(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            at=clock_in + timedelta(hours=2, minutes=30),
            reason="Back",
            by_user_id=test_user["id"],
        )

    end_rows = [r for r in await _rows(att_id) if r.action == "break_end"]
    end_fields = _by_field(end_rows)
    assert end_fields["status"].original_value == "on_break"
    assert end_fields["status"].corrected_value == "working"
    assert end_fields["break_end_at"].original_value == tl.NONE
    assert end_fields["break_end_at"].target_id is not None


# ── 3. break 세션 CUD — 예전엔 이력이 아예 안 남던 경로 ───────────────────


async def test_break_add_update_delete_all_recorded(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """콘솔 휴식 편집(추가/수정/삭제)이 전부 이력에 남는다.

    이 경로는 이전에 attendance_breaks 만 고치고 이력을 하나도 남기지 않았다.
    """
    clock_in = datetime.combine(_today(), time(9, 0), tzinfo=timezone.utc)
    att_id = await _make_attendance(
        test_user, test_store_id, status="working", clock_in=clock_in
    )
    started = clock_in + timedelta(hours=3)
    ended = started + timedelta(minutes=30)
    org_id = test_user["organization_id"]

    # (a) 추가 — 없음 → 값
    async with async_session() as db:
        created = await attendance_service.add_break(
            db,
            attendance_id=att_id,
            organization_id=org_id,
            started_at=started,
            ended_at=ended,
            break_type=BREAK_TYPE_UNPAID_MEAL,
            reason="Missed punch",
            by_user_id=test_user["id"],
        )
        break_id = created.id

    added = _by_field([r for r in await _rows(att_id) if r.action == "break_added"])
    assert added, "break 추가가 이력에 남지 않았다"
    assert added["break_start_at"].original_value == tl.NONE
    assert added["break_type"].corrected_value == BREAK_TYPE_UNPAID_MEAL
    assert all(r.reason == "Missed punch" for r in added.values())
    assert all(str(r.target_id) == str(break_id) for r in added.values())

    # (b) 수정 — 시간과 타입 변경, before 는 이전 값
    async with async_session() as db:
        await attendance_service.update_break(
            db,
            attendance_id=att_id,
            break_id=break_id,
            organization_id=org_id,
            started_at=started + timedelta(minutes=5),
            ended_at=None,
            break_type=BREAK_TYPE_PAID_10MIN,
            clear_ended_at=False,
            reason="Break correction",
            by_user_id=test_user["id"],
        )

    updated = _by_field([r for r in await _rows(att_id) if r.action == "break_updated"])
    assert updated["break_start_at"].original_value == started.isoformat()
    assert updated["break_type"].original_value == BREAK_TYPE_UNPAID_MEAL
    assert updated["break_type"].corrected_value == BREAK_TYPE_PAID_10MIN
    # 안 바뀐 종료시각은 행을 만들지 않는다 (변화 없으면 소음)
    assert "break_end_at" not in updated

    # (c) 삭제 — 값 → 없음. 세션 행이 사라져도 무엇을 지웠는지 남는다
    async with async_session() as db:
        await attendance_service.delete_break(
            db,
            attendance_id=att_id,
            break_id=break_id,
            organization_id=org_id,
            reason="Duplicate",
            by_user_id=test_user["id"],
        )

    removed = _by_field([r for r in await _rows(att_id) if r.action == "break_removed"])
    assert removed, "break 삭제가 이력에 남지 않았다"
    assert removed["break_start_at"].corrected_value == tl.NONE
    assert removed["break_type"].original_value == BREAK_TYPE_PAID_10MIN
    assert removed["break_type"].corrected_value == tl.NONE


async def test_break_change_without_reason_records_placeholder(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """사유는 선택 — 안 넣어도 기록되고 "(no reason)" 으로 남는다."""
    clock_in = datetime.combine(_today(), time(9, 0), tzinfo=timezone.utc)
    att_id = await _make_attendance(
        test_user, test_store_id, status="working", clock_in=clock_in
    )
    async with async_session() as db:
        await attendance_service.add_break(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            started_at=clock_in + timedelta(hours=2),
            ended_at=None,
            break_type=BREAK_TYPE_PAID_10MIN,
        )
    rows = [r for r in await _rows(att_id) if r.action == "break_added"]
    assert rows
    assert all(r.reason == tl.NO_REASON for r in rows)


# ── 4. 정정 — 비어 있던 값의 before 도 센티널로 남는다 ────────────────────


async def test_correction_on_empty_field_records_sentinel_before(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """비어 있던 note 를 채우면 before 는 NULL 이 아니라 (empty) 로 남는다."""
    att_id = await _make_attendance(test_user, test_store_id)

    async with async_session() as db:
        await attendance_service.correct_attendance(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            field_name="note",
            corrected_value="Left early for dentist",
            reason="Note added",
            corrected_by=test_user["id"],
        )

    rows = await _rows(att_id)
    note_row = _by_field(rows)["note"]
    assert note_row.original_value == tl.EMPTY
    assert note_row.corrected_value == "Left early for dentist"
    assert note_row.action == "modify"
    assert note_row.group_id is not None


# ── 5. 사유 편집은 그룹 전체에 반영 ───────────────────────────────────────


async def test_reason_update_applies_to_whole_group(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """한 행의 사유를 고치면 같은 액션의 다른 행도 함께 바뀐다."""
    att_id = await _make_attendance(test_user, test_store_id)
    at = datetime.combine(_today(), time(9, 2), tzinfo=timezone.utc)
    async with async_session() as db:
        await attendance_action_service.clock_in(
            db,
            attendance_id=att_id,
            organization_id=test_user["organization_id"],
            at=at,
            reason="Original",
            by_user_id=test_user["id"],
        )

    rows = await _rows(att_id)
    assert len(rows) == 2

    async with async_session() as db:
        await attendance_service.update_correction_reason(
            db,
            attendance_id=att_id,
            correction_id=rows[0].id,
            organization_id=test_user["organization_id"],
            reason="Forgot to punch in",
        )

    after = await _rows(att_id)
    assert {r.reason for r in after} == {"Forgot to punch in"}
