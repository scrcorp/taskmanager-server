"""Unit — 스케줄 변경 시 라벨 재판정 (`relabel_after_schedule_change`).

원인 B: 17시 스케줄에 15시 출근(early) 을 찍은 뒤 스케줄을 14시로 고쳐도 라벨이
"조기 출근" 인 채로 얼어붙어 있었다. 여기서 지키는 것은 세 가지다.

1. 새 스케줄 기준으로 라벨이 다시 매겨진다 (early ↔ late ↔ 정시).
2. 판정이 소유하지 않는 라벨(auto_clocked_out 등)은 **절대 사라지지 않는다** —
   사라지면 payroll 게이트가 조용히 통과한다.
3. 판정 불가(스케줄 시각 미상 / 미퇴근)일 때는 기존 라벨을 보존한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.attendance_lifecycle_service import (
    RELABEL_OWNED_ANOMALIES,
    relabel_after_schedule_change,
)
from app.services.attendance_threshold_service import AttendanceThresholds

# 기본 임계값 = 시드 기본값 (late 5분 / early clock-in 10분 / early leave 10분)
T = AttendanceThresholds(late_buffer=5, early_clock_in=10, early_leave=10)

UTC = timezone.utc


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 13, hour, minute, second, tzinfo=UTC)


def _relabel(**kw):
    """기본값이 있는 얇은 래퍼 — 테스트마다 바꾸는 인자만 쓰게 한다."""
    params = {
        "status": "working",
        "clock_in": _at(15),
        "clock_out": None,
        "anomalies": None,
        "total_work_minutes": None,
        "scheduled_start": _at(17),
        "scheduled_end": _at(21),
        "thresholds": T,
    }
    params.update(kw)
    return relabel_after_schedule_change(**params)


# ── clock-in 계열 ─────────────────────────────────────────────────────────


def test_early_becomes_late_when_schedule_moved_earlier():
    """17시 스케줄에 15시 출근(early) → 스케줄 14시로 변경 → late 로 뒤집힌다."""
    status, anomalies = _relabel(
        status="working",
        anomalies=["early_clock_in_override"],
        scheduled_start=_at(14),
        scheduled_end=_at(21),
    )
    assert status == "late"
    assert anomalies == ["late"]


def test_late_becomes_on_time_when_schedule_moved_later():
    """지각으로 찍힌 건이 스케줄을 늦추면 정시가 된다."""
    status, anomalies = _relabel(
        status="late",
        clock_in=_at(15),
        anomalies=["late"],
        scheduled_start=_at(15),
    )
    assert status == "working"
    assert anomalies is None


def test_on_time_becomes_early_when_schedule_moved_later():
    """정시였던 출근이 스케줄을 뒤로 미루면 조기 출근이 된다."""
    status, anomalies = _relabel(
        status="working",
        clock_in=_at(15),
        anomalies=None,
        scheduled_start=_at(17),
    )
    assert status == "working"
    assert anomalies == ["early_clock_in_override"]


def test_late_buffer_boundary_is_minute_floored():
    """buffer 5분 → 17:05:59 는 지각이 아니고 17:06:00 부터 지각 (초 버림)."""
    _, ok = _relabel(clock_in=_at(17, 5, 59), anomalies=["late"])
    assert ok is None
    status, late = _relabel(clock_in=_at(17, 6, 0), anomalies=None)
    assert status == "late"
    assert late == ["late"]


def test_early_threshold_boundary():
    """early 10분 → 16:50 은 정시, 16:49 는 조기 출근."""
    _, ok = _relabel(clock_in=_at(16, 50))
    assert ok is None
    _, early = _relabel(clock_in=_at(16, 49))
    assert early == ["early_clock_in_override"]


def test_no_show_never_survives_on_a_row_with_clock_in():
    """출근 기록이 있는 이상 no_show 는 성립하지 않는다 — 소유 라벨이라 걷힌다."""
    _, anomalies = _relabel(anomalies=["no_show"], scheduled_start=_at(15))
    assert anomalies is None


def test_unknown_schedule_start_preserves_clock_in_labels():
    """스케줄 시작 시각을 모르면 판정하지 않는다 — 기존 라벨을 그대로 둔다."""
    status, anomalies = _relabel(
        status="late",
        anomalies=["late"],
        scheduled_start=None,
        scheduled_end=None,
    )
    assert status == "late"  # 근거 없이 working 으로 승격하지 않는다
    assert anomalies == ["late"]


# ── 소유하지 않는 라벨 보존 (payroll 게이트 방어) ─────────────────────────


def test_unowned_anomalies_are_never_dropped():
    """auto_clocked_out / no_break / overlapping_clock_in 은 재판정이 손대지 않는다.

    사라지면 payroll 게이트 ①(미확인 자동퇴근)이 아무 말 없이 통과한다.
    """
    _, anomalies = _relabel(
        anomalies=["auto_clocked_out", "no_break", "overlapping_clock_in", "late"],
        scheduled_start=_at(14),  # → late 유지
    )
    assert anomalies is not None
    assert "auto_clocked_out" in anomalies
    assert "no_break" in anomalies
    assert "overlapping_clock_in" in anomalies
    # 보존 라벨이 앞, 새로 판정된 소유 라벨이 뒤
    assert anomalies[:3] == ["auto_clocked_out", "no_break", "overlapping_clock_in"]


def test_owned_set_is_exactly_the_schedule_derived_labels():
    """소유 집합이 조용히 넓어지면 보존 라벨이 사라진다 — 목록을 고정한다."""
    assert RELABEL_OWNED_ANOMALIES == {
        "late",
        "no_show",
        "early_clock_in_override",
        "early_leave",
        "early_clock_out",
        "overtime",
    }
    for preserved in ("auto_clocked_out", "no_break", "overlapping_clock_in"):
        assert preserved not in RELABEL_OWNED_ANOMALIES


# ── clock-out 계열 ────────────────────────────────────────────────────────


def test_early_leave_is_recomputed_on_clock_out_rows():
    """종료를 뒤로 미루면 정시 퇴근이 조퇴가 된다."""
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(21),
        anomalies=None,
        total_work_minutes=240,
        scheduled_start=_at(17),
        scheduled_end=_at(23),
    )
    assert anomalies == ["early_leave"]


def test_early_leave_is_cleared_when_schedule_end_moved_earlier():
    """종료를 앞당기면 조퇴 라벨이 걷힌다."""
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(20),
        anomalies=["early_leave"],
        total_work_minutes=180,
        scheduled_start=_at(17),
        scheduled_end=_at(20),
    )
    assert anomalies is None


def test_existing_early_clock_out_code_is_not_normalized():
    """키오스크가 붙인 `early_clock_out` 은 그대로 유지한다 (콘솔 배지/필터 보호)."""
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(20),
        anomalies=["early_clock_out"],
        total_work_minutes=180,
        scheduled_start=_at(17),
        scheduled_end=_at(23),
    )
    assert anomalies == ["early_clock_out"]
    assert "early_leave" not in anomalies


def test_overtime_follows_new_schedule_length():
    """스케줄 길이를 줄이면 같은 근무가 초과근무가 된다 (예정 + 30분 기준)."""
    # 예정 4시간(240분), 실근무 300분 → 240+30=270 초과 → overtime
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(22),
        anomalies=None,
        total_work_minutes=300,
        scheduled_start=_at(17),
        scheduled_end=_at(21),
    )
    assert anomalies == ["overtime"]

    # 예정을 5시간으로 늘리면 같은 근무가 더 이상 초과근무가 아니다
    _, anomalies2 = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(22),
        anomalies=["overtime"],
        total_work_minutes=300,
        scheduled_start=_at(17),
        scheduled_end=_at(22),
    )
    assert anomalies2 is None


def test_open_shift_preserves_clock_out_labels():
    """미퇴근 row 의 조퇴/초과근무 라벨은 판정 대상이 아니다 — 그대로 둔다."""
    _, anomalies = _relabel(
        status="working",
        clock_in=_at(17),
        clock_out=None,
        anomalies=["early_leave", "overtime"],
        scheduled_start=_at(17),
    )
    assert anomalies == ["early_leave", "overtime"]


def test_overtime_preserved_when_length_unknown():
    """예정 길이를 모르면 초과근무를 판정할 수 없다 — 기존 값을 보존한다."""
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=_at(17),
        clock_out=_at(22),
        anomalies=["overtime"],
        total_work_minutes=None,
        scheduled_start=_at(17),
        scheduled_end=_at(21),
    )
    assert anomalies == ["overtime"]


# ── status 취급 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["clocked_out", "on_break"])
def test_status_not_invented_for_finished_or_break_rows(status: str):
    """퇴근/휴식 중 row 의 status 는 사건이 정한다 — 재판정이 바꾸지 않는다."""
    new_status, _ = _relabel(
        status=status,
        clock_in=_at(19),
        clock_out=_at(21) if status == "clocked_out" else None,
        total_work_minutes=120 if status == "clocked_out" else None,
        scheduled_start=_at(17),
        scheduled_end=_at(21),
    )
    assert new_status == status


def test_clock_times_are_inputs_only():
    """이 함수는 라벨만 돌려준다 — 시각을 바꿀 통로 자체가 없다."""
    result = _relabel(clock_in=_at(15), clock_out=_at(19), total_work_minutes=240)
    assert isinstance(result, tuple) and len(result) == 2


def test_relabel_is_idempotent():
    """같은 입력으로 두 번 돌려도 결과가 흔들리지 않는다 (이력 소음 방지의 전제)."""
    first = _relabel(anomalies=["auto_clocked_out"], scheduled_start=_at(14))
    second = _relabel(
        status=first[0], anomalies=first[1], scheduled_start=_at(14)
    )
    assert first == second


def test_early_and_late_are_mutually_exclusive():
    """이르면 early 로 확정 — 두 라벨이 함께 붙지 않는다."""
    _, anomalies = _relabel(clock_in=_at(10), scheduled_start=_at(17))
    assert anomalies == ["early_clock_in_override"]
    assert "late" not in anomalies


def test_zero_thresholds_are_honored():
    """임계값 0 매장에서는 1분만 늦어도 지각, 1분만 일러도 조기 출근."""
    zero = AttendanceThresholds(late_buffer=0, early_clock_in=0, early_leave=0)
    _, late = _relabel(clock_in=_at(17, 1), scheduled_start=_at(17), thresholds=zero)
    assert late == ["late"]
    _, early = _relabel(
        clock_in=_at(16, 59), scheduled_start=_at(17), thresholds=zero
    )
    assert early == ["early_clock_in_override"]


def test_seconds_are_floored_not_rounded():
    """17:00:30 출근은 buffer 0 매장에서도 지각이 아니다 (같은 분)."""
    zero = AttendanceThresholds(late_buffer=0, early_clock_in=0, early_leave=0)
    _, anomalies = _relabel(
        clock_in=_at(17, 0, 30), scheduled_start=_at(17), thresholds=zero
    )
    assert anomalies is None


def test_overnight_schedule_shift():
    """자정을 넘긴 스케줄도 절대시각끼리 비교되므로 그대로 판정된다."""
    start = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=8)
    _, anomalies = _relabel(
        status="clocked_out",
        clock_in=start + timedelta(minutes=30),
        clock_out=end,
        total_work_minutes=450,
        scheduled_start=start,
        scheduled_end=end,
    )
    assert anomalies == ["late"]
