"""Unit tests — payroll_period_service 반월 캘린더 헬퍼 (Payroll v1 Phase 2).

DB 없음 — 순수 함수 검증:
    - period_bounds_for: 1~15 / 16~말일, 2월(평년/윤년) EOM, 15/16 경계, 12월 말
    - prev_period_bounds: 후반→전반, 전반→전월 후반, 연 경계, 2월 경계
    - week_start_for / workweeks_touching: Sun–Sat 고정(C3),
      기간 경계에 걸친 주 포함(C4), 단일일 기간, 역순 입력 거부
    - period_bounds_for == tip_service.cycle_for_date (공식 단일화 — C6)
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.payroll_period_service import (
    period_bounds_for,
    prev_period_bounds,
    week_start_for,
    workweeks_touching,
)
from app.services.tip_service import cycle_for_date


# ── period_bounds_for ─────────────────────────────────────────


class TestPeriodBoundsFor:
    def test_first_half(self) -> None:
        assert period_bounds_for(date(2026, 8, 7)) == (date(2026, 8, 1), date(2026, 8, 15))

    def test_day_15_belongs_to_first_half(self) -> None:
        assert period_bounds_for(date(2026, 8, 15)) == (date(2026, 8, 1), date(2026, 8, 15))

    def test_day_16_starts_second_half(self) -> None:
        assert period_bounds_for(date(2026, 8, 16)) == (date(2026, 8, 16), date(2026, 8, 31))

    def test_feb_non_leap_eom(self) -> None:
        # 2026 은 평년 — 2/28 이 말일
        assert period_bounds_for(date(2026, 2, 20)) == (date(2026, 2, 16), date(2026, 2, 28))

    def test_feb_leap_eom(self) -> None:
        # 2028 은 윤년 — 2/29 이 말일
        assert period_bounds_for(date(2028, 2, 16)) == (date(2028, 2, 16), date(2028, 2, 29))

    def test_december_second_half(self) -> None:
        assert period_bounds_for(date(2025, 12, 31)) == (date(2025, 12, 16), date(2025, 12, 31))

    def test_matches_tip_cycle_formula(self) -> None:
        # C6: pay period == 팁 사이클. 공식이 갈라지면 안 된다.
        for d in (
            date(2026, 1, 1), date(2026, 2, 28), date(2026, 6, 15),
            date(2026, 6, 16), date(2028, 2, 29), date(2025, 12, 31),
        ):
            assert period_bounds_for(d) == cycle_for_date(d)


# ── prev_period_bounds ────────────────────────────────────────


class TestPrevPeriodBounds:
    def test_second_half_to_first_half(self) -> None:
        assert prev_period_bounds(date(2026, 8, 20)) == (date(2026, 8, 1), date(2026, 8, 15))

    def test_first_half_to_prev_month_second_half(self) -> None:
        assert prev_period_bounds(date(2026, 8, 3)) == (date(2026, 7, 16), date(2026, 7, 31))

    def test_year_boundary(self) -> None:
        assert prev_period_bounds(date(2026, 1, 5)) == (date(2025, 12, 16), date(2025, 12, 31))

    def test_march_back_into_feb_non_leap(self) -> None:
        assert prev_period_bounds(date(2026, 3, 1)) == (date(2026, 2, 16), date(2026, 2, 28))

    def test_march_back_into_feb_leap(self) -> None:
        assert prev_period_bounds(date(2028, 3, 10)) == (date(2028, 2, 16), date(2028, 2, 29))


# ── week_start_for / workweeks_touching ──────────────────────


class TestWeekStartFor:
    def test_sunday_is_own_week_start(self) -> None:
        # 2026-08-16 은 일요일
        assert week_start_for(date(2026, 8, 16)) == date(2026, 8, 16)

    def test_saturday_maps_to_preceding_sunday(self) -> None:
        # 2026-08-01 은 토요일 → 주 시작은 7/26(일)
        assert week_start_for(date(2026, 8, 1)) == date(2026, 7, 26)

    def test_all_days_of_one_week_share_start(self) -> None:
        start = date(2026, 8, 2)  # 일요일
        for offset in range(7):
            assert week_start_for(start + timedelta(days=offset)) == start


class TestWorkweeksTouching:
    def test_first_half_straddles_left_edge(self) -> None:
        # 8/1(토) 이 속한 주는 7/26~8/1 — 전기(7월 후반)에 걸친 주도 포함
        weeks = workweeks_touching(date(2026, 8, 1), date(2026, 8, 15))
        assert weeks == [
            (date(2026, 7, 26), date(2026, 8, 1)),
            (date(2026, 8, 2), date(2026, 8, 8)),
            (date(2026, 8, 9), date(2026, 8, 15)),
        ]

    def test_second_half_straddles_right_edge(self) -> None:
        # 8/31(월) 이 속한 주는 8/30~9/5 — 차기(9월 전반)에 걸친 주도 포함
        weeks = workweeks_touching(date(2026, 8, 16), date(2026, 8, 31))
        assert weeks == [
            (date(2026, 8, 16), date(2026, 8, 22)),
            (date(2026, 8, 23), date(2026, 8, 29)),
            (date(2026, 8, 30), date(2026, 9, 5)),
        ]

    def test_feb_second_half_ends_clean_saturday(self) -> None:
        # 2/28(토) 이 주 끝과 일치 — 오른쪽 straddle 없음
        weeks = workweeks_touching(date(2026, 2, 16), date(2026, 2, 28))
        assert weeks == [
            (date(2026, 2, 15), date(2026, 2, 21)),
            (date(2026, 2, 22), date(2026, 2, 28)),
        ]

    def test_all_weeks_are_sun_sat(self) -> None:
        for ws, we in workweeks_touching(date(2026, 1, 1), date(2026, 3, 31)):
            assert ws.weekday() == 6  # 일요일
            assert we.weekday() == 5  # 토요일
            assert we - ws == timedelta(days=6)

    def test_single_day_period(self) -> None:
        weeks = workweeks_touching(date(2026, 8, 5), date(2026, 8, 5))
        assert weeks == [(date(2026, 8, 2), date(2026, 8, 8))]

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            workweeks_touching(date(2026, 8, 15), date(2026, 8, 1))
