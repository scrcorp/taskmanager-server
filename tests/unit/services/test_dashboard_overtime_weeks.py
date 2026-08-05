"""Unit tests — dashboard 주간 초과근무 집계 (P0-4, DB 없음).

week_start_of / build_weekly_overtime_rows 검증:
    - 주는 일요일 시작 Sun→Sat (경계 귀속 포함)
    - user × 주 단위 net 합산 — 범위 전체 합계를 한 주 한도와 비교하던 버그 방지
    - net 은 C1 공식(compute_net_work_minutes) — gross 판정 금지
    - 멀티 매장 주간은 min() 한도 (보수적), 매장 미상은 기본 40h

분기 커버:
    - 멀티 주 범위: 45h 주만 초과, 20h 주는 0 (row 는 둘 다 존재 — 시트 포함 규칙 유지)
    - 일요일/토요일 경계 귀속
    - gross 초과 / net 이내 → 미초과
    - 멀티 매장 min 한도
    - store None / map 미포함 → 기본 40h
    - 진행 중(total_work_minutes None) attendance → 0 기여
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.models.attendance import Attendance
from app.models.attendance_break import (
    BREAK_TYPE_UNPAID_MEAL,
    AttendanceBreak,
)
from app.services.dashboard_service import build_weekly_overtime_rows, week_start_of
from app.services.labor_law_service import DEFAULT_MAX_WEEKLY_HOURS


# 고정 주: 2026-05-03(일) ~ 05-09(토), 다음 주 05-10(일) ~ 05-16(토)
WEEK1_START = date(2026, 5, 3)
WEEK2_START = date(2026, 5, 10)

_BASE = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)


def _att(
    user_id: UUID,
    work_date: date,
    total_work_minutes: int | None,
    store_id: UUID | None = None,
) -> Attendance:
    """transient Attendance 생성 헬퍼 (DB 미사용)."""
    return Attendance(
        id=uuid4(),
        user_id=user_id,
        store_id=store_id,
        work_date=work_date,
        total_work_minutes=total_work_minutes,
    )


def _unpaid_break(duration: int) -> AttendanceBreak:
    return AttendanceBreak(
        id=uuid4(),
        attendance_id=uuid4(),
        started_at=_BASE,
        ended_at=_BASE + timedelta(minutes=duration),
        break_type=BREAK_TYPE_UNPAID_MEAL,
        duration_minutes=duration,
    )


# ── week_start_of — Sun→Sat 주 규칙 ────────────────────────────────


def test_week_start_of_sunday_is_itself() -> None:
    assert week_start_of(date(2026, 5, 3)) == date(2026, 5, 3)


def test_week_start_of_saturday_is_previous_sunday() -> None:
    """토요일은 그 주(6일 전 일요일)에 귀속 — 다음 주 아님."""
    assert week_start_of(date(2026, 5, 9)) == date(2026, 5, 3)


def test_week_start_of_midweek() -> None:
    assert week_start_of(date(2026, 5, 6)) == date(2026, 5, 3)


# ── build_weekly_overtime_rows ──────────────────────────────────────


def test_multi_week_only_over_threshold_week_flagged() -> None:
    """45h 주는 overtime 5.0, 20h 주는 0.0 — 주 단위로 각각 판정.

    기존 버그였다면 65h 합계 vs 40h 로 25h 초과가 나왔을 케이스.
    """
    uid = uuid4()
    sid = uuid4()
    attendances = [
        # week1: 5일 × 540분 = 2700분 = 45h
        *[_att(uid, WEEK1_START + timedelta(days=i), 540, sid) for i in range(5)],
        # week2: 3일 × 400분 = 1200분 = 20h
        *[_att(uid, WEEK2_START + timedelta(days=i), 400, sid) for i in range(3)],
    ]
    rows = build_weekly_overtime_rows(attendances, {}, {sid: 40})

    assert len(rows) == 2
    week1, week2 = rows
    assert week1["week_start"] == WEEK1_START
    assert week1["week_end"] == WEEK1_START + timedelta(days=6)
    assert week1["total_hours"] == 45.0
    assert week1["max_weekly"] == 40
    assert week1["overtime_hours"] == 5.0
    # 20h 주는 row 는 존재하되 (시트 포함 규칙 유지) 초과 0
    assert week2["week_start"] == WEEK2_START
    assert week2["total_hours"] == 20.0
    assert week2["overtime_hours"] == 0.0


def test_boundary_sunday_goes_to_new_week_saturday_to_old() -> None:
    """일요일 attendance 는 새 주, 그 전날 토요일은 이전 주에 귀속."""
    uid = uuid4()
    sid = uuid4()
    attendances = [
        _att(uid, date(2026, 5, 9), 300, sid),   # 토 → week1
        _att(uid, date(2026, 5, 10), 200, sid),  # 일 → week2
    ]
    rows = build_weekly_overtime_rows(attendances, {}, {sid: 40})

    assert [(r["week_start"], r["total_hours"]) for r in rows] == [
        (WEEK1_START, 5.0),
        (WEEK2_START, round(200 / 60, 1)),
    ]


def test_net_not_gross_for_comparison() -> None:
    """gross 41h / net 39h → 미초과 (unpaid break 차감 반영)."""
    uid = uuid4()
    sid = uuid4()
    attendances = []
    breaks_map: dict[UUID, list[AttendanceBreak]] = {}
    for i in range(6):
        # 일 410분 gross + unpaid 20 → net 390. 주간 net 2340분 = 39h (gross 41h)
        att = _att(uid, WEEK1_START + timedelta(days=i), 410, sid)
        attendances.append(att)
        breaks_map[att.id] = [_unpaid_break(20)]

    rows = build_weekly_overtime_rows(attendances, breaks_map, {sid: 40})

    assert len(rows) == 1
    assert rows[0]["total_hours"] == 39.0
    assert rows[0]["overtime_hours"] == 0.0


def test_multi_store_week_uses_min_threshold() -> None:
    """한 주에 두 매장 근무 시 가장 엄격한(작은) 한도로 판정."""
    uid = uuid4()
    store_a, store_b = uuid4(), uuid4()
    attendances = [
        *[_att(uid, WEEK1_START + timedelta(days=i), 420, store_a) for i in range(3)],
        *[_att(uid, WEEK1_START + timedelta(days=3 + i), 420, store_b) for i in range(2)],
    ]  # 주간 2100분 = 35h
    rows = build_weekly_overtime_rows(
        attendances, {}, {store_a: 30, store_b: 45}
    )

    assert len(rows) == 1
    assert rows[0]["max_weekly"] == 30
    assert rows[0]["overtime_hours"] == 5.0


def test_store_none_or_unknown_falls_back_to_default() -> None:
    """store 유실(None) row 또는 map 미포함 매장 → 기본 40h."""
    uid_lost, uid_unknown = uuid4(), uuid4()
    unknown_sid = uuid4()
    attendances = [
        _att(uid_lost, WEEK1_START, 2520),                 # store None, 42h
        _att(uid_unknown, WEEK1_START, 2520, unknown_sid),  # map 미포함, 42h
    ]
    rows = build_weekly_overtime_rows(attendances, {}, {})

    assert len(rows) == 2
    for row in rows:
        assert row["max_weekly"] == DEFAULT_MAX_WEEKLY_HOURS
        assert row["overtime_hours"] == 2.0


def test_in_progress_attendance_contributes_zero() -> None:
    """clock-out 전(total_work_minutes None) row 는 0분으로 집계."""
    uid = uuid4()
    sid = uuid4()
    attendances = [
        _att(uid, WEEK1_START, 2460, sid),      # 41h
        _att(uid, WEEK1_START + timedelta(days=1), None, sid),  # 진행 중
    ]
    rows = build_weekly_overtime_rows(attendances, {}, {sid: 40})

    assert len(rows) == 1
    assert rows[0]["total_hours"] == 41.0
    assert rows[0]["overtime_hours"] == 1.0
