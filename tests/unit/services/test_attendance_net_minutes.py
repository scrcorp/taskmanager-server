"""Unit tests — compute_net_work_minutes / summarize_break_sessions (P0-1, 결정 C1).

순 근무시간 단일 공식 검증 (DB 없음, transient ORM 객체 사용):
    net = max(0, total_work_minutes - unpaid break 전체 - paid break 세션별 10분 초과분)

분기 커버:
    - break 없음
    - unpaid meal 만 (전체 차감)
    - paid 정확히 10분 (차감 없음)
    - paid 10분 초과 (초과분만 차감)
    - paid 다중 세션 (세션별 초과분 합산)
    - 진행 중(open, ended_at NULL) break (집계 제외)
    - total_work_minutes None / 0
    - 하한 clamp (음수 방지)
    - 레거시 break_type (paid_short / unpaid_long)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.attendance import Attendance
from app.models.attendance_break import (
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
    BREAK_TYPE_UNPAID_MEAL,
    AttendanceBreak,
)
from app.services.attendance_service import (
    compute_net_work_minutes,
    summarize_break_sessions,
)


_BASE = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _brk(break_type: str, duration: int | None, *, open_session: bool = False) -> AttendanceBreak:
    """transient AttendanceBreak 생성 헬퍼 (DB 미사용)."""
    return AttendanceBreak(
        id=uuid4(),
        attendance_id=uuid4(),
        started_at=_BASE,
        ended_at=None if open_session else _BASE + timedelta(minutes=duration or 0),
        break_type=break_type,
        duration_minutes=None if open_session else duration,
    )


def _att(total_work_minutes: int | None) -> Attendance:
    """transient Attendance 생성 헬퍼 (DB 미사용)."""
    return Attendance(total_work_minutes=total_work_minutes)


# ── compute_net_work_minutes ────────────────────────────────────────


def test_no_breaks_net_equals_total_work() -> None:
    assert compute_net_work_minutes(_att(480), []) == 480


def test_unpaid_meal_deducted_fully() -> None:
    breaks = [_brk(BREAK_TYPE_UNPAID_MEAL, 30)]
    assert compute_net_work_minutes(_att(480), breaks) == 450


def test_paid_break_exactly_10_not_deducted() -> None:
    """paid 10분 이내는 유급 — 순 근무시간에서 차감하지 않는다."""
    breaks = [_brk(BREAK_TYPE_PAID_10MIN, 10)]
    assert compute_net_work_minutes(_att(480), breaks) == 480


def test_paid_break_under_10_not_deducted() -> None:
    breaks = [_brk(BREAK_TYPE_PAID_10MIN, 7)]
    assert compute_net_work_minutes(_att(480), breaks) == 480


def test_paid_break_overage_deducted() -> None:
    """paid 15분 → 초과 5분만 차감."""
    breaks = [_brk(BREAK_TYPE_PAID_10MIN, 15)]
    assert compute_net_work_minutes(_att(480), breaks) == 475


def test_multiple_paid_sessions_overage_per_session() -> None:
    """세션별 초과분 계산 — 15분(+5), 12분(+2), 8분(+0) → 7분 차감."""
    breaks = [
        _brk(BREAK_TYPE_PAID_10MIN, 15),
        _brk(BREAK_TYPE_PAID_10MIN, 12),
        _brk(BREAK_TYPE_PAID_10MIN, 8),
    ]
    assert compute_net_work_minutes(_att(480), breaks) == 473


def test_mixed_unpaid_and_paid_overage() -> None:
    """unpaid 30 전체 + paid 15 초과 5 → 35분 차감."""
    breaks = [
        _brk(BREAK_TYPE_UNPAID_MEAL, 30),
        _brk(BREAK_TYPE_PAID_10MIN, 15),
    ]
    assert compute_net_work_minutes(_att(480), breaks) == 445


def test_open_break_ignored() -> None:
    """진행 중(ended_at NULL) break 는 차감하지 않는다 (현행 동작 유지)."""
    breaks = [_brk(BREAK_TYPE_UNPAID_MEAL, None, open_session=True)]
    assert compute_net_work_minutes(_att(480), breaks) == 480


def test_none_total_work_returns_none() -> None:
    """clock-out 전 (total_work_minutes None) → None."""
    breaks = [_brk(BREAK_TYPE_UNPAID_MEAL, 30)]
    assert compute_net_work_minutes(_att(None), breaks) is None


def test_zero_total_work_returns_zero() -> None:
    breaks = [_brk(BREAK_TYPE_UNPAID_MEAL, 30)]
    assert compute_net_work_minutes(_att(0), breaks) == 0


def test_clamped_at_zero_when_breaks_exceed_work() -> None:
    """차감이 근무시간보다 크면 0 하한."""
    breaks = [_brk(BREAK_TYPE_UNPAID_MEAL, 90)]
    assert compute_net_work_minutes(_att(60), breaks) == 0


def test_legacy_break_types_classified() -> None:
    """레거시 paid_short/unpaid_long 도 동일 분류 — 480 - 20(unpaid_long) - 5(paid_short 15) = 455."""
    breaks = [
        _brk(BREAK_TYPE_PAID_SHORT, 15),
        _brk(BREAK_TYPE_UNPAID_LONG, 20),
    ]
    assert compute_net_work_minutes(_att(480), breaks) == 455


# ── summarize_break_sessions ────────────────────────────────────────


def test_summarize_totals_and_overage() -> None:
    breaks = [
        _brk(BREAK_TYPE_PAID_10MIN, 15),
        _brk(BREAK_TYPE_PAID_10MIN, 8),
        _brk(BREAK_TYPE_UNPAID_MEAL, 30),
    ]
    paid, unpaid, overage, items = summarize_break_sessions(breaks)
    assert paid == 23
    assert unpaid == 30
    assert overage == 5
    assert len(items) == 3


def test_summarize_open_break_in_items_but_not_totals() -> None:
    """open 세션은 items 에는 포함되지만 paid/unpaid/overage 집계에서 제외."""
    breaks = [
        _brk(BREAK_TYPE_PAID_10MIN, 15),
        _brk(BREAK_TYPE_UNPAID_MEAL, None, open_session=True),
    ]
    paid, unpaid, overage, items = summarize_break_sessions(breaks)
    assert paid == 15
    assert unpaid == 0
    assert overage == 5
    assert len(items) == 2
    assert items[1]["ended_at"] is None


def test_summarize_empty() -> None:
    assert summarize_break_sessions([]) == (0, 0, 0, [])
