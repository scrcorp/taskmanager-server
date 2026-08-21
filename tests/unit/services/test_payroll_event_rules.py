"""Unit — payroll penalty 감지 순수 규칙 (Payroll v1 Phase 2).

대상:
    - app/core/payroll_rules.py: required_rest_breaks 단계 경계
    - app/services/payroll_event_service.py: evaluate_meal_penalty /
      evaluate_rest_penalty (presence 기반 v1 규칙, 레거시 break type 포함)
    - PayrollEventService._classification_reason (분류 이벤트 reason 포맷)

DB 없음 — AttendanceBreak 는 비영속 ORM 인스턴스로 구성.
스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §6, 설계방향 C5.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from app.core.payroll_rules import required_meal_breaks, required_rest_breaks
from app.models.attendance_break import (
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
    BREAK_TYPE_UNPAID_MEAL,
    AttendanceBreak,
)
from app.services.payroll_event_service import (
    ClassifiedDay,
    evaluate_meal_penalty,
    evaluate_rest_penalty,
    payroll_event_service,
)


_BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _br(
    break_type: str,
    minutes: int | None = 10,
    *,
    open_session: bool = False,
) -> AttendanceBreak:
    """비영속 break 세션. open_session=True 면 진행 중 (ended_at/duration NULL)."""
    if open_session:
        return AttendanceBreak(
            attendance_id=uuid.uuid4(),
            started_at=_BASE,
            ended_at=None,
            break_type=break_type,
            duration_minutes=None,
        )
    return AttendanceBreak(
        attendance_id=uuid.uuid4(),
        started_at=_BASE,
        ended_at=_BASE + timedelta(minutes=minutes or 0),
        break_type=break_type,
        duration_minutes=minutes,
    )


# ---------------------------------------------------------------------------
# required_rest_breaks — 단계 경계
# ---------------------------------------------------------------------------


def test_required_rest_breaks_tiers() -> None:
    """< 3.5h → 0 / 3.5–6h → 1 / >6–10h → 2 / >10h → 3 (경계 포함 검증)."""
    assert required_rest_breaks(0) == 0
    assert required_rest_breaks(209) == 0   # 3.5h 미만
    assert required_rest_breaks(210) == 1   # 3.5h 정확히 — 1회 시작
    assert required_rest_breaks(360) == 1   # 6h 정확히 — 아직 1회
    assert required_rest_breaks(361) == 2   # 6h 초과 — 2회
    assert required_rest_breaks(600) == 2   # 10h 정확히 — 아직 2회
    assert required_rest_breaks(601) == 3   # 10h 초과 — 3회


# ---------------------------------------------------------------------------
# evaluate_meal_penalty
# ---------------------------------------------------------------------------


def test_meal_no_penalty_in_waiver_band() -> None:
    """5h 초과~6h 이하는 blanket waiver 로 면제 — 위반 아님."""
    assert evaluate_meal_penalty(301, []) is None
    assert evaluate_meal_penalty(360, []) is None


def test_meal_penalty_just_over_six_hours() -> None:
    """net 361분 + meal break 없음 → 위반 (waiver 는 6h 초과 시 무효)."""
    reason = evaluate_meal_penalty(361, [])
    assert reason is not None


def test_meal_reason_format() -> None:
    """reason 은 영어 + 시간 표기 + 몇 개 중 몇 개인지 (372분 → 6.20h)."""
    reason = evaluate_meal_penalty(372, [])
    assert reason == "Worked 6.20h with 0 of 1 required 30-min meal break(s)"


def test_meal_satisfied_by_30min_unpaid_meal() -> None:
    """완료된 30분 무급 meal 세션이 있으면 위반 아님."""
    assert evaluate_meal_penalty(400, [_br(BREAK_TYPE_UNPAID_MEAL, 30)]) is None


def test_meal_29min_session_insufficient() -> None:
    """29분 meal 은 30분 미만 — 여전히 위반."""
    assert evaluate_meal_penalty(400, [_br(BREAK_TYPE_UNPAID_MEAL, 29)]) is not None


def test_meal_two_short_sessions_do_not_sum() -> None:
    """20분 meal 두 번(합 40분)은 불인정 — 세션 단위 30분 이상이어야."""
    breaks = [_br(BREAK_TYPE_UNPAID_MEAL, 20), _br(BREAK_TYPE_UNPAID_MEAL, 20)]
    assert evaluate_meal_penalty(400, breaks) is not None


def test_meal_legacy_unpaid_long_recognized() -> None:
    """레거시 unpaid_long 도 무급 meal 로 인정 (dual-read)."""
    assert evaluate_meal_penalty(400, [_br(BREAK_TYPE_UNPAID_LONG, 45)]) is None


def test_meal_paid_break_does_not_satisfy() -> None:
    """유급 휴게 30분은 meal 아님 — 위반 유지."""
    assert evaluate_meal_penalty(400, [_br(BREAK_TYPE_PAID_10MIN, 30)]) is not None


def test_meal_open_session_not_counted() -> None:
    """진행 중(ended_at NULL) meal 세션은 duration 미확정 — 불인정."""
    assert (
        evaluate_meal_penalty(400, [_br(BREAK_TYPE_UNPAID_MEAL, open_session=True)])
        is not None
    )


# ---------------------------------------------------------------------------
# evaluate_rest_penalty
# ---------------------------------------------------------------------------


def test_rest_no_penalty_under_threshold() -> None:
    """net < 3.5h 는 휴게 의무 없음."""
    assert evaluate_rest_penalty(209, []) is None


def test_rest_penalty_zero_of_one() -> None:
    """net 3.5h + 유급 세션 0개 → 위반, reason 에 0 of 1."""
    reason = evaluate_rest_penalty(210, [])
    assert reason == "Worked 3.50h with 0 of 1 required 10-min rest break(s)"


def test_rest_satisfied_one_session() -> None:
    """3.5–6h 구간은 유급 세션 1개로 충족."""
    assert evaluate_rest_penalty(210, [_br(BREAK_TYPE_PAID_10MIN, 10)]) is None


def test_rest_penalty_one_of_two() -> None:
    """>6h 는 2개 필요 — 1개면 위반 (1 of 2)."""
    reason = evaluate_rest_penalty(400, [_br(BREAK_TYPE_PAID_10MIN, 10)])
    assert reason is not None
    assert "1 of 2" in reason


def test_rest_satisfied_two_sessions() -> None:
    """>6–10h 구간은 2개로 충족."""
    breaks = [_br(BREAK_TYPE_PAID_10MIN, 10), _br(BREAK_TYPE_PAID_10MIN, 10)]
    assert evaluate_rest_penalty(400, breaks) is None


def test_rest_over_ten_hours_needs_three() -> None:
    """>10h 는 3개 필요 — 2개면 위반 (2 of 3)."""
    breaks = [_br(BREAK_TYPE_PAID_10MIN, 10), _br(BREAK_TYPE_PAID_10MIN, 10)]
    reason = evaluate_rest_penalty(601, breaks)
    assert reason is not None
    assert "2 of 3" in reason


def test_rest_legacy_paid_short_counts() -> None:
    """레거시 paid_short 세션도 유급 휴게로 카운트."""
    assert evaluate_rest_penalty(210, [_br(BREAK_TYPE_PAID_SHORT, 10)]) is None


def test_rest_unpaid_sessions_do_not_count() -> None:
    """무급 meal 세션은 rest 카운트 대상 아님."""
    assert evaluate_rest_penalty(210, [_br(BREAK_TYPE_UNPAID_MEAL, 30)]) is not None


def test_rest_open_session_not_counted() -> None:
    """진행 중 유급 세션은 완료 전 — 카운트 제외."""
    assert (
        evaluate_rest_penalty(210, [_br(BREAK_TYPE_PAID_10MIN, open_session=True)])
        is not None
    )


# ---------------------------------------------------------------------------
# _classification_reason — 분류 이벤트 reason 포맷
# ---------------------------------------------------------------------------


def _day(**overrides) -> ClassifiedDay:
    kwargs = dict(user_id=uuid.uuid4(), work_date=date(2026, 8, 3))
    kwargs.update(overrides)
    return ClassifiedDay(**kwargs)


def test_classification_reason_daily_ot_only() -> None:
    day = _day(daily_ot_minutes=150)
    reason = payroll_event_service._classification_reason(day, "daily_ot")
    assert reason == "Daily overtime: 2.5h over 8h/day at 1.5x"


def test_classification_reason_daily_ot_with_dt() -> None:
    day = _day(daily_ot_minutes=240, daily_dt_minutes=30)
    reason = payroll_event_service._classification_reason(day, "daily_ot")
    assert reason == (
        "Daily overtime: 4.0h over 8h/day at 1.5x, 0.5h over 12h/day at 2x"
    )


def test_classification_reason_weekly_ot() -> None:
    day = _day(weekly_ot_minutes=90)
    reason = payroll_event_service._classification_reason(day, "weekly_ot")
    assert reason == "Weekly overtime: 1.5h over 40h/week at 1.5x"


def test_classification_reason_seventh_day() -> None:
    day = _day(seventh_day=True)
    reason = payroll_event_service._classification_reason(day, "seventh_day")
    assert reason == "7th consecutive day worked in Sun-Sat workweek"


# ── §512(a) meal 면제 / 2회차 ────────────────────────────────────────


def test_no_meal_required_up_to_six_hours() -> None:
    """5h 초과라도 총 6h 이하면 상호 합의로 면제 가능 — 위반이 아니다.

    이걸 빼먹으면 5~6h 근무가 전부 위반으로 잡힌다 (실측 833건).
    """
    for minutes in (301, 330, 359, 360):
        assert required_meal_breaks(minutes) == 0
        assert evaluate_meal_penalty(minutes, []) is None


def test_meal_required_just_past_six_hours() -> None:
    """6시간 1분부터는 면제가 안 된다."""
    assert required_meal_breaks(361) == 1
    assert evaluate_meal_penalty(361, []) is not None


def test_second_meal_required_only_past_twelve_hours() -> None:
    """10~12h 는 2회차를 면제 가능 — 1회면 충분. 12h 초과부터 2회."""
    assert required_meal_breaks(11 * 60) == 1
    assert required_meal_breaks(12 * 60) == 1
    assert required_meal_breaks(12 * 60 + 1) == 2


def test_one_meal_is_not_enough_past_twelve_hours() -> None:
    breaks = [_br(BREAK_TYPE_UNPAID_MEAL, 30)]
    assert evaluate_meal_penalty(13 * 60, breaks) is not None
    assert evaluate_meal_penalty(12 * 60, breaks) is None


def test_two_meals_satisfy_a_long_day() -> None:
    breaks = [_br(BREAK_TYPE_UNPAID_MEAL, 30), _br(BREAK_TYPE_UNPAID_MEAL, 30)]
    assert evaluate_meal_penalty(13 * 60, breaks) is None


def test_short_meal_does_not_count() -> None:
    """30분 미만은 인정하지 않는다."""
    assert evaluate_meal_penalty(8 * 60, [_br(BREAK_TYPE_UNPAID_MEAL, 29)]) is not None
    assert evaluate_meal_penalty(8 * 60, [_br(BREAK_TYPE_UNPAID_MEAL, 30)]) is None


def test_rest_breaks_reach_four_past_fourteen_hours() -> None:
    assert required_rest_breaks(14 * 60) == 3
    assert required_rest_breaks(14 * 60 + 1) == 4
