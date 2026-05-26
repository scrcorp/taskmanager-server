"""Unit tests — attendance_service module (pure functions / no DB).

[작성됨] — 이번 phase (Phase 3 보정)
- compute_effective_status (8가지 분기 + overnight shift)

[작성 필요] — 추후
- AttendanceService.scan / build_response / build_correction_response
- AttendanceService.correct_attendance / get_corrections / count_corrections_by_ids
- _add_anomaly / _find_schedule_for_attendance (DB 의존, integration 위주)

DB 사용하는 케이스는 tests/integration/services/test_attendance_service.py 에.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.services.attendance_service import compute_effective_status


# ── 분기 매핑 (compute_effective_status docstring 참조) ──────────
# 1. clock_in 있음           → DB status 그대로
# 2. status not in {upcoming, late} → 그대로
# 3. schedule 정보 부족     → 그대로
# 4. sched_end 지남          → no_show
# 5. 이미 late               → late
# 6. start + late_buffer 지남 → late 전이
# 7. start - soon_threshold 이내 → soon 전이
# 8. 그 외                   → upcoming


_UTC = ZoneInfo("UTC")
_TODAY = date(2026, 5, 22)


def _now(hour: int, minute: int = 0) -> datetime:
    """UTC 시각 헬퍼."""
    return datetime(2026, 5, 22, hour, minute, tzinfo=timezone.utc)


# ── 분기 1: clock_in 있음 → DB status 그대로 (강등 금지) ──────


def test_clock_in_present_returns_db_status_even_after_sched_end() -> None:
    """clock_in 이 있으면 sched_end 지나도 status 그대로 (no_show 강등 X)."""
    result = compute_effective_status(
        att_status="working",
        att_clock_in=_now(9, 5),
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(18, 0),  # sched_end (17:00) 이미 지남
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "working"


def test_clock_in_present_with_late_status_promotes_to_working() -> None:
    """clock_in 있는데 status=late 이면 'working' 으로 승격.
    effective_status 의 'late' 는 미출근 지각 한정. 출근 후 지각 마킹은 anomalies 로."""
    result = compute_effective_status(
        att_status="late",
        att_clock_in=_now(9, 30),
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(10, 0),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "working"


# ── 분기 2: status not in {upcoming, late} → 그대로 ───────────


def test_working_status_returned_unchanged() -> None:
    result = compute_effective_status(
        att_status="working",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(10, 0),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "working"


def test_on_break_status_returned_unchanged() -> None:
    result = compute_effective_status(
        att_status="on_break",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(12, 0),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "on_break"


# ── 분기 3: schedule 정보 부족 → 그대로 ──────────────────────


def test_upcoming_without_schedule_start_returns_upcoming() -> None:
    """schedule.start_time None → 그대로 (계산 불가)."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=None,
        schedule_end_time=None,
        schedule_work_date=None,
        now=_now(10, 0),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "upcoming"


# ── 분기 4: sched_end 지났음 → no_show ────────────────────────


def test_upcoming_after_sched_end_becomes_no_show() -> None:
    """clock_in 없는 upcoming/late + sched_end 지남 → no_show."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(17, 30),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "no_show"


def test_late_after_sched_end_becomes_no_show() -> None:
    """이미 late persisted + sched_end 지남 → no_show."""
    result = compute_effective_status(
        att_status="late",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(18, 0),
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "no_show"


# ── 분기 5: 이미 late → late (sched_end 안 지난 경우만 도달) ─


def test_persisted_late_stays_late_before_sched_end() -> None:
    """status=late + clock_in 없음 + sched_end 안 지남 → late 유지."""
    result = compute_effective_status(
        att_status="late",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(10, 0),  # sched_end (17:00) 안 지남
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "late"


# ── 분기 6: now >= start + late_buffer → late 전이 ────────────


def test_upcoming_after_buffer_becomes_late() -> None:
    """now > start + late_buffer → late."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(9, 10),  # start=9:00 + buffer=5 < 9:10
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "late"


# ── 분기 7: start - soon_threshold ≤ now < start + buffer → soon ─


def test_upcoming_near_start_becomes_soon() -> None:
    """now 가 start 직전 (soon_threshold 이내) → soon."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(8, 57),  # start=9:00 - soon(5분) ≤ 8:57 < 9:00 + buffer(5분)
        store_tz=_UTC,
        late_buffer=5,
        soon_threshold_minutes=5,
    )
    assert result == "soon"


def test_upcoming_within_buffer_after_start_stays_soon() -> None:
    """now 가 start 지났지만 buffer 안 → soon (아직 late 아님)."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(9, 3),  # 9:00 ≤ 9:03 < 9:05
        store_tz=_UTC,
        late_buffer=5,
        soon_threshold_minutes=5,
    )
    assert result == "soon"


# ── 분기 8: now < start - soon_threshold → upcoming ──────────


def test_upcoming_far_before_start_stays_upcoming() -> None:
    """now 가 start 보다 soon_threshold 이상 일찍 → upcoming."""
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(9, 0),
        schedule_end_time=time(17, 0),
        schedule_work_date=_TODAY,
        now=_now(8, 30),  # start=9:00 - soon(5분)=8:55 > 8:30
        store_tz=_UTC,
        late_buffer=5,
        soon_threshold_minutes=5,
    )
    assert result == "upcoming"


# ── 추가: overnight shift (end < start, 다음날 보정) ─────────


def test_overnight_shift_sched_end_next_day_no_show() -> None:
    """end_time(02:00) < start_time(21:00) → sched_end 가 다음날로 보정.
    다음날 02:00 지나면 no_show.
    """
    # work_date = 2026-05-22 (오늘), shift = 21:00 ~ 02:00 (다음날 새벽)
    # 다음날 03:00 → no_show
    next_day_3am = datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc)
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(21, 0),
        schedule_end_time=time(2, 0),  # 다음날 새벽 의도
        schedule_work_date=_TODAY,
        now=next_day_3am,
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "no_show"


def test_overnight_shift_within_window_stays_late() -> None:
    """overnight shift 진행 중 (start 지남, end 안 지남) + clock_in 없으면 late."""
    # shift 21:00 ~ 다음날 02:00. 23:00 → late (start+buffer 지남)
    night_11pm = datetime(2026, 5, 22, 23, 0, tzinfo=timezone.utc)
    result = compute_effective_status(
        att_status="upcoming",
        att_clock_in=None,
        schedule_start_time=time(21, 0),
        schedule_end_time=time(2, 0),
        schedule_work_date=_TODAY,
        now=night_11pm,
        store_tz=_UTC,
        late_buffer=5,
    )
    assert result == "late"
