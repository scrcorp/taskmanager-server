"""Unit — payroll_confirm_service 순수 규칙 (Payroll v1 Phase 3).

대상: app/services/payroll_confirm_service.py
    - verify_row_consistency: breakdown 합계 == 스칼라 검증 (스펙 §5)
    - evaluate_org_week_minutes: 멀티스토어 주간 정합 (계산 규칙 2)

DB 없음 — 계약 모델을 직접 조립해 분기 전부 커버.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.core.payroll_rules import EVENT_KIND_MEAL_PENALTY
from app.schemas.payroll import (
    DayDetail,
    EntryBreakdown,
    PayrollPreviewRow,
    PenaltyLine,
    RateSegment,
)
from app.services.payroll_confirm_service import (
    ORG_WEEK_DAILY_OVER_8H,
    ORG_WEEK_OVER_40H,
    ORG_WEEK_SEVENTH_DAY_SPLIT,
    evaluate_org_week_minutes,
    verify_row_consistency,
)

_MON = date(2026, 7, 6)
_SUN = date(2026, 7, 5)


# ---------------------------------------------------------------------------
# verify_row_consistency
# ---------------------------------------------------------------------------


def _consistent_row(**overrides) -> PayrollPreviewRow:
    """일관된 기준 행 — Mon 10h(8h reg + 2h OT) @ $20 + penalty $20 + tips $50.

    reg $160.00 + ot $60.00 + penalty $20.00 + tips $50.00 = gross $290.00.
    """
    base = dict(
        user_id=uuid4(),
        member_name="Unit Row",
        regular_minutes=480,
        ot_minutes=120,
        dt_minutes=0,
        regular_pay=Decimal("160.00"),
        ot_pay=Decimal("60.00"),
        dt_pay=Decimal("0.00"),
        penalty_pay=Decimal("20.00"),
        card_tips=Decimal("50.00"),
        gross_pay=Decimal("290.00"),
        breakdown=EntryBreakdown(
            segments=[
                RateSegment(
                    rate=Decimal("20.00"),
                    regular_minutes=480,
                    ot_minutes=120,
                    dt_minutes=0,
                    amount=Decimal("220.00"),
                )
            ],
            days=[
                DayDetail(
                    work_date=_MON,
                    regular_minutes=480,
                    ot_minutes=120,
                    dt_minutes=0,
                    applied_rate=Decimal("20.00"),
                )
            ],
            penalties=[
                PenaltyLine(
                    work_date=_MON,
                    kind=EVENT_KIND_MEAL_PENALTY,
                    reason="Worked 10.0h with no 30-min meal break",
                    amount=Decimal("20.00"),
                )
            ],
        ),
    )
    base.update(overrides)
    return PayrollPreviewRow(**base)


def test_consistent_row_passes() -> None:
    assert verify_row_consistency(_consistent_row()) == []


def test_zero_row_passes() -> None:
    """빈 breakdown + 전부 0 스칼라 — 활동 없는 행도 일관."""
    row = _consistent_row(
        regular_minutes=0, ot_minutes=0, dt_minutes=0,
        regular_pay=Decimal("0.00"), ot_pay=Decimal("0.00"),
        dt_pay=Decimal("0.00"), penalty_pay=Decimal("0.00"),
        card_tips=Decimal("0.00"), gross_pay=Decimal("0.00"),
        breakdown=EntryBreakdown(),
    )
    assert verify_row_consistency(row) == []


def test_day_minutes_mismatch_detected() -> None:
    """days 합 != 스칼라 분 — day/segment 양쪽에서 잡힌다."""
    row = _consistent_row(regular_minutes=400)
    problems = verify_row_consistency(row)
    assert any("day minutes" in p for p in problems)
    assert any("segment minutes" in p for p in problems)


def test_segment_amount_mismatch_detected() -> None:
    """segment amount 합 != reg+ot+dt pay."""
    row = _consistent_row(ot_pay=Decimal("61.00"), gross_pay=Decimal("291.00"))
    problems = verify_row_consistency(row)
    assert any("segment amount" in p for p in problems)


def test_penalty_total_mismatch_detected() -> None:
    row = _consistent_row(penalty_pay=Decimal("25.00"), gross_pay=Decimal("295.00"))
    problems = verify_row_consistency(row)
    assert any("penalty line total" in p for p in problems)


def test_gross_mismatch_detected() -> None:
    row = _consistent_row(gross_pay=Decimal("300.00"))
    problems = verify_row_consistency(row)
    assert any("gross_pay" in p for p in problems)


# ---------------------------------------------------------------------------
# evaluate_org_week_minutes
# ---------------------------------------------------------------------------

_STORE_A = UUID("00000000-0000-0000-0000-00000000000a")
_STORE_B = UUID("00000000-0000-0000-0000-00000000000b")


def _days(start: date, minutes: list[int]) -> dict[date, int]:
    from datetime import timedelta

    return {
        start + timedelta(days=i): m for i, m in enumerate(minutes) if m or m == 0
    }


def _kinds(violations: list[tuple[str, str]]) -> list[str]:
    return [kind for kind, _ in violations]


def test_single_store_never_flagged() -> None:
    """매장 1곳이면 50h 라도 위반 아님 — 매장 단위 엔진이 정확히 처리."""
    minutes = {_STORE_A: _days(_SUN, [600, 600, 600, 600, 600])}  # 50h
    assert evaluate_org_week_minutes(minutes) == []


def test_zero_minute_store_not_counted() -> None:
    """0분 매장은 활성 매장으로 안 센다 (사실상 단일 매장)."""
    minutes = {
        _STORE_A: {_MON: 2700},  # 45h 하루? — 어쨌든 단일 매장
        _STORE_B: {_MON + (date(2026, 7, 7) - _MON): 0},
    }
    assert evaluate_org_week_minutes(minutes) == []


def test_two_stores_over_40h_flagged() -> None:
    """A 24h + B 21h = 45h > 40h — weekly 위반 1건 (겹치는 날 없음)."""
    minutes = {
        _STORE_A: _days(_MON, [480, 480, 480]),  # Mon-Wed 24h
        _STORE_B: _days(date(2026, 7, 9), [660, 600]),  # Thu 11h, Fri 10h
    }
    violations = evaluate_org_week_minutes(minutes)
    assert _kinds(violations) == [ORG_WEEK_OVER_40H]
    assert "45.0" in violations[0][1]
    assert "40" in violations[0][1]


def test_two_stores_under_40h_not_flagged() -> None:
    """A 16h + B 8h = 24h, 겹치는 날 없음 — 위반 없음."""
    minutes = {
        _STORE_A: _days(_MON, [480, 480]),
        _STORE_B: {date(2026, 7, 8): 480},
    }
    assert evaluate_org_week_minutes(minutes) == []


def test_seventh_day_split_flagged() -> None:
    """org 관점 7일 연속(35h)인데 어느 매장도 7일 전부는 아님 — 7일 위반."""
    minutes = {
        _STORE_A: _days(_SUN, [300, 300, 300, 300]),  # Sun-Wed
        _STORE_B: _days(date(2026, 7, 9), [300, 300, 300]),  # Thu-Sat
    }
    violations = evaluate_org_week_minutes(minutes)
    assert _kinds(violations) == [ORG_WEEK_SEVENTH_DAY_SPLIT]


def test_seventh_day_single_store_covers_it() -> None:
    """한 매장이 7일 전부 근무 — 그 매장 엔진이 처리하므로 위반 아님."""
    minutes = {
        _STORE_A: _days(_SUN, [300, 300, 300, 300, 300, 300, 300]),  # 35h
        _STORE_B: {_MON: 60},  # 36h 합산 — 40h 미만, 일 합산 6h
    }
    assert evaluate_org_week_minutes(minutes) == []


def test_daily_overlap_over_8h_flagged() -> None:
    """같은 날 두 매장 5h+5h = 10h > 8h — daily 위반."""
    minutes = {
        _STORE_A: {_MON: 300},
        _STORE_B: {_MON: 300},
    }
    violations = evaluate_org_week_minutes(minutes)
    assert _kinds(violations) == [ORG_WEEK_DAILY_OVER_8H]
    assert str(_MON) in violations[0][1]


def test_daily_overlap_under_8h_not_flagged() -> None:
    """같은 날 두 매장 3h+3h = 6h — 위반 없음."""
    minutes = {
        _STORE_A: {_MON: 180},
        _STORE_B: {_MON: 180},
    }
    assert evaluate_org_week_minutes(minutes) == []


def test_multiple_violations_reported_together() -> None:
    """주간 초과 + 일 합산 초과가 동시에 — 둘 다 보고."""
    minutes = {
        _STORE_A: _days(_MON, [480, 480, 480, 300]),  # Mon-Wed 8h, Thu 5h
        _STORE_B: {
            date(2026, 7, 9): 300,  # Thu 5h — Thu 합산 10h
            date(2026, 7, 10): 600,  # Fri 10h — 주 합산 44h
        },
    }
    kinds = _kinds(evaluate_org_week_minutes(minutes))
    assert ORG_WEEK_OVER_40H in kinds
    assert ORG_WEEK_DAILY_OVER_8H in kinds
