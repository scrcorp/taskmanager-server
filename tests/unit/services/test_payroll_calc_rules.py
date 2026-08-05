"""Unit — payroll 계산 엔진 순수 분류 규칙 (Payroll v1 Phase 2).

대상: app/services/payroll_calc_service.py (DB 없음)
    - classify_week: CA 병합 규칙 (C2) — 일별 OT/DT, 7일 연속, 주간 40h,
      이중계상 금지, frozen 통과, 경계값(정확히 8h/12h/40h), 0분 일
    - ot_base_rate_for_week: 멀티 rate 주 가중평균 (계산 규칙 1)
    - day_amounts: 일별 금액 공식 (구간 누적과 같은 공식 — 반올림 시점만 다름)
    - allocate_penalty_hours: 일 상한 2h 클램프 (C5)
    - parse_frozen_breakdown / frozen_day_to_week_day: calc_version 계약
      + 금액 필드 없던 옛 동결본 하위호환 파싱

스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §5·계산 규칙 1~3, 설계방향 C2~C4.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas.payroll import CALC_VERSION, ContextDay, DayDetail, EntryBreakdown
from app.services.payroll_calc_service import (
    DayClassification,
    WeekDay,
    allocate_penalty_hours,
    classify_week,
    day_amounts,
    frozen_day_to_week_day,
    ot_base_rate_for_week,
    parse_frozen_breakdown,
)
from app.utils.exceptions import BadRequestError

# 기준 주 — 2026-07-05 는 일요일 (Sun–Sat: 7/5 ~ 7/11)
_SUN = date(2026, 7, 5)
_MON = date(2026, 7, 6)
_TUE = date(2026, 7, 7)
_WED = date(2026, 7, 8)
_THU = date(2026, 7, 9)
_FRI = date(2026, 7, 10)
_SAT = date(2026, 7, 11)

_R20 = Decimal("20.00")


def _d(work_date: date, minutes: int, rate: Decimal = _R20) -> WeekDay:
    return WeekDay(work_date=work_date, net_minutes=minutes, rate=rate)


def _by_date(days):
    return {d.work_date: d for d in days}


# ---------------------------------------------------------------------------
# classify_week — 일별 규칙 (C2 ①)
# ---------------------------------------------------------------------------


class TestDailyClassification:
    def test_empty_input(self) -> None:
        assert classify_week([]) == []

    def test_under_8h_all_regular(self) -> None:
        (day,) = classify_week([_d(_MON, 360)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (360, 0, 0)

    def test_exactly_8h_no_ot(self) -> None:
        """경계: 정확히 8h(480분)는 OT 없음 (초과분만 OT)."""
        (day,) = classify_week([_d(_MON, 480)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (480, 0, 0)
        assert day.daily_ot_minutes == 0

    def test_one_minute_over_8h(self) -> None:
        (day,) = classify_week([_d(_MON, 481)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (480, 1, 0)

    def test_10h_day(self) -> None:
        """10h = 8 reg + 2 daily OT."""
        (day,) = classify_week([_d(_MON, 600)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (480, 120, 0)
        assert day.daily_ot_minutes == 120

    def test_exactly_12h_no_dt(self) -> None:
        """경계: 정확히 12h(720분)는 DT 없음 — OT 4h 까지만."""
        (day,) = classify_week([_d(_MON, 720)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (480, 240, 0)
        assert day.daily_dt_minutes == 0

    def test_13h_day(self) -> None:
        """13h = 8 reg + 4 OT + 1 DT."""
        (day,) = classify_week([_d(_MON, 780)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (480, 240, 60)
        assert day.daily_ot_minutes == 240
        assert day.daily_dt_minutes == 60

    def test_zero_minute_day(self) -> None:
        """0분 일 — 전부 0, 근무일로 세지 않는다 (7일 연속 판정 참조)."""
        (day,) = classify_week([_d(_MON, 0)])
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (0, 0, 0)


# ---------------------------------------------------------------------------
# classify_week — 주간 40h 규칙 (C2 ③)
# ---------------------------------------------------------------------------


class TestWeeklyOvertime:
    def test_exactly_40h_no_weekly_ot(self) -> None:
        """경계: 정확히 40h(5×8h)는 weekly OT 없음."""
        days = classify_week([_d(_SUN + timedelta(days=i), 480) for i in range(5)])
        assert all(d.weekly_ot_minutes == 0 for d in days)
        assert sum(d.regular_minutes for d in days) == 2400

    def test_42h_puts_2h_weekly_ot_on_last_day(self) -> None:
        """6×7h=42h → 마지막 날에만 2h weekly OT (앞 5일은 온전 regular)."""
        days = classify_week([_d(_SUN + timedelta(days=i), 420) for i in range(6)])
        by = _by_date(days)
        for i in range(5):
            assert by[_SUN + timedelta(days=i)].weekly_ot_minutes == 0
        last = by[_FRI]
        assert last.weekly_ot_minutes == 120
        assert last.regular_minutes == 300
        assert last.ot_minutes == 120  # 병합 최종값에도 반영

    def test_no_double_counting_daily_ot_hours(self) -> None:
        """이중계상 금지: 일별 OT/DT 로 이미 분류된 분은 40h 카운트 제외.

        4×10h = 40h gross 지만 straight-time 은 32h — weekly OT 없음.
        """
        days = classify_week([_d(_SUN + timedelta(days=i), 600) for i in range(4)])
        assert all(d.weekly_ot_minutes == 0 for d in days)
        assert sum(d.daily_ot_minutes for d in days) == 480

    def test_weekly_ot_spans_multiple_days(self) -> None:
        """40h 초과 시점 이후 straight-time 은 전부 weekly OT (여러 날에 걸쳐도).

        5×8h(=40h) + 금 6h + 토 3h → 금 6h·토 3h 전부 weekly OT.
        토는 7일째 (7일 연속) 라서 이 케이스가 안 되므로 일요일은 쉬는 구성:
        월~금 8h(40h) + 토 3h → 토 3h 전부 weekly OT.
        """
        days = classify_week(
            [_d(_MON + timedelta(days=i), 480) for i in range(5)] + [_d(_SAT, 180)]
        )
        by = _by_date(days)
        assert by[_SAT].weekly_ot_minutes == 180
        assert by[_SAT].regular_minutes == 0
        assert by[_FRI].weekly_ot_minutes == 0


# ---------------------------------------------------------------------------
# classify_week — 7일 연속 규칙 (C2 ②)
# ---------------------------------------------------------------------------


class TestSeventhDay:
    def test_seventh_day_under_8h_all_ot(self) -> None:
        """7일 전부 근무, 7일째 6h → 6h 전부 1.5x (OT), seventh_day 플래그."""
        days = classify_week(
            [_d(_SUN + timedelta(days=i), 360) for i in range(7)]
        )
        by = _by_date(days)
        sat = by[_SAT]
        assert sat.seventh_day is True
        assert (sat.regular_minutes, sat.ot_minutes, sat.dt_minutes) == (0, 360, 0)
        assert sat.daily_ot_minutes == 0  # 일별 규칙 자체는 미발동 (이벤트 표기용)
        # 앞 6일은 7일 규칙 영향 없음
        assert by[_SUN].seventh_day is False
        assert by[_SUN].regular_minutes == 360

    def test_seventh_day_over_8h_gets_dt(self) -> None:
        """7일째 10h → 8h OT + 2h DT (8h 초과분 2x)."""
        days = classify_week(
            [_d(_SUN + timedelta(days=i), 360) for i in range(6)] + [_d(_SAT, 600)]
        )
        sat = _by_date(days)[_SAT]
        assert sat.seventh_day is True
        assert (sat.regular_minutes, sat.ot_minutes, sat.dt_minutes) == (0, 480, 120)
        # 일별 규칙 성분은 순수값 유지 (10h → daily OT 2h)
        assert sat.daily_ot_minutes == 120
        assert sat.daily_dt_minutes == 0

    def test_not_seventh_when_a_day_missed(self) -> None:
        """6일 근무(수요일 쉼)면 토요일은 7일째 아님."""
        dates = [_SUN, _MON, _TUE, _THU, _FRI, _SAT]
        days = classify_week([_d(d, 360) for d in dates])
        assert all(d.seventh_day is False for d in days)

    def test_zero_minute_day_breaks_consecutive_chain(self) -> None:
        """0분 일은 근무일이 아니다 — 7일 연속 판정에서 제외."""
        days = classify_week(
            [_d(_SUN + timedelta(days=i), 360) for i in range(6)] + [_d(_SAT, 0)]
        )
        assert all(d.seventh_day is False for d in days)
        days2 = classify_week(
            [_d(_SUN, 0)] + [_d(_SUN + timedelta(days=i), 360) for i in range(1, 7)]
        )
        assert all(d.seventh_day is False for d in days2)

    def test_seventh_day_hours_do_not_feed_weekly_counter(self) -> None:
        """7일째는 전부 OT/DT — straight-time 40h 카운터에 안 들어간다.

        6×6h(36h) + 7일째 6h → 주 42h 지만 weekly OT 0 (7일째 6h 는 이미 OT).
        """
        days = classify_week([_d(_SUN + timedelta(days=i), 360) for i in range(7)])
        assert all(d.weekly_ot_minutes == 0 for d in days)


# ---------------------------------------------------------------------------
# classify_week — frozen 통과 + straddle (계산 규칙 3 / C4)
# ---------------------------------------------------------------------------


class TestFrozenDays:
    def test_frozen_day_passthrough(self) -> None:
        """frozen 일은 재분류 없이 그대로 (13h 라도 frozen 값 유지)."""
        frozen = WeekDay(
            work_date=_MON, net_minutes=780, rate=_R20,
            frozen=True, frozen_regular=780, frozen_ot=0, frozen_dt=0,
        )
        (day,) = classify_week([frozen])
        assert day.frozen is True
        assert (day.regular_minutes, day.ot_minutes, day.dt_minutes) == (780, 0, 0)

    def test_frozen_straight_time_feeds_weekly_counter(self) -> None:
        """A2 straddle: 전기 frozen Sun–Thu 8h×5(40h) → 현기 Fri 8h 전부 weekly OT."""
        frozen = [
            WeekDay(
                work_date=_SUN + timedelta(days=i), net_minutes=480, rate=_R20,
                frozen=True, frozen_regular=480,
            )
            for i in range(5)
        ]
        days = classify_week(frozen + [_d(_FRI, 480)])
        fri = _by_date(days)[_FRI]
        assert fri.weekly_ot_minutes == 480
        assert fri.regular_minutes == 0

    def test_frozen_ot_hours_do_not_feed_weekly_counter(self) -> None:
        """frozen 일의 OT 분은 straight-time 이 아니다 (이중계상 금지 유지)."""
        frozen = [
            WeekDay(
                work_date=_SUN + timedelta(days=i), net_minutes=600, rate=_R20,
                frozen=True, frozen_regular=480, frozen_ot=120,
            )
            for i in range(4)  # straight 32h + OT 8h
        ]
        days = classify_week(frozen + [_d(_THU, 480)])  # straight 누적 40h 정확히
        thu = _by_date(days)[_THU]
        assert thu.weekly_ot_minutes == 0
        assert thu.regular_minutes == 480

    def test_frozen_worked_days_count_for_seventh(self) -> None:
        """7일 연속 판정에 frozen 근무일 포함 — 7일째(live)가 승격된다."""
        frozen = [
            WeekDay(
                work_date=_SUN + timedelta(days=i), net_minutes=360, rate=_R20,
                frozen=True, frozen_regular=360,
            )
            for i in range(6)
        ]
        days = classify_week(frozen + [_d(_SAT, 360)])
        sat = _by_date(days)[_SAT]
        assert sat.seventh_day is True
        assert (sat.regular_minutes, sat.ot_minutes) == (0, 360)


# ---------------------------------------------------------------------------
# classify_week — 입력 검증
# ---------------------------------------------------------------------------


class TestClassifyWeekValidation:
    def test_rejects_cross_week_input(self) -> None:
        with pytest.raises(ValueError, match="single Sun-Sat week"):
            classify_week([_d(_SAT, 60), _d(_SAT + timedelta(days=1), 60)])

    def test_rejects_duplicate_dates(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            classify_week([_d(_MON, 60), _d(_MON, 120)])


# ---------------------------------------------------------------------------
# ot_base_rate_for_week — 가중평균 (계산 규칙 1)
# ---------------------------------------------------------------------------


class TestOtBaseRate:
    def test_single_rate_returns_none(self) -> None:
        """rate 1개 → None (그날 rate 사용)."""
        assert ot_base_rate_for_week([_d(_MON, 480), _d(_TUE, 600)]) is None

    def test_no_rated_days_returns_none(self) -> None:
        days = [WeekDay(work_date=_MON, net_minutes=480, rate=None)]
        assert ot_base_rate_for_week(days) is None

    def test_weighted_average(self) -> None:
        """2일 8h@20 + 1일 10h@24 → (960×20+600×24)/1560 = 21.5384..."""
        days = [
            _d(_MON, 480, Decimal("20")),
            _d(_TUE, 480, Decimal("20")),
            _d(_WED, 600, Decimal("24")),
        ]
        base = ot_base_rate_for_week(days)
        assert base is not None
        expected = (Decimal(960) * 20 + Decimal(600) * 24) / Decimal(1560)
        assert base == expected

    def test_zero_and_none_rate_days_excluded(self) -> None:
        """rate 미상(None/0) 일은 표본 제외 — 남은 rate 가 1개면 None."""
        days = [
            _d(_MON, 480, Decimal("20")),
            WeekDay(work_date=_TUE, net_minutes=480, rate=None),
            _d(_WED, 480, Decimal("0")),
        ]
        assert ot_base_rate_for_week(days) is None

    def test_zero_minute_days_excluded(self) -> None:
        """0분 일은 가중치 0 이므로 표본 제외 (rate 만 다른 0분 일 무시)."""
        days = [_d(_MON, 480, Decimal("20")), _d(_TUE, 0, Decimal("24"))]
        assert ot_base_rate_for_week(days) is None


# ---------------------------------------------------------------------------
# allocate_penalty_hours — 일 상한 (C5)
# ---------------------------------------------------------------------------


class TestPenaltyHours:
    def test_zero_events(self) -> None:
        assert allocate_penalty_hours(0) == []

    def test_one_event(self) -> None:
        assert allocate_penalty_hours(1) == [1]

    def test_two_events_reach_cap(self) -> None:
        assert allocate_penalty_hours(2) == [1, 1]

    def test_third_event_clamped_to_zero(self) -> None:
        """상한 2h 초과분은 0h (라인은 유지 — 사유 표시용)."""
        assert allocate_penalty_hours(3) == [1, 1, 0]


# ---------------------------------------------------------------------------
# day_amounts — 일별 금액 공식 (구간 누적과 공유)
# ---------------------------------------------------------------------------


class TestDayAmounts:
    @staticmethod
    def _day(regular: int = 0, ot: int = 0, dt: int = 0) -> DayClassification:
        return DayClassification(
            work_date=_MON,
            net_minutes=regular + ot + dt,
            regular_minutes=regular,
            ot_minutes=ot,
            dt_minutes=dt,
        )

    def test_regular_only(self) -> None:
        reg, ot, dt = day_amounts(self._day(regular=480), _R20, _R20)
        assert reg == Decimal("160")
        assert (ot, dt) == (Decimal("0"), Decimal("0"))

    def test_ot_is_one_and_half_times_base(self) -> None:
        _, ot, _ = day_amounts(self._day(regular=480, ot=120), _R20, _R20)
        assert ot == Decimal("60")  # 2h × 1.5 × $20

    def test_dt_is_double_base(self) -> None:
        _, _, dt = day_amounts(self._day(regular=480, ot=240, dt=60), _R20, _R20)
        assert dt == Decimal("40")  # 1h × 2 × $20

    def test_premium_uses_week_base_not_day_rate(self) -> None:
        """멀티 rate 주 — premium base 는 가중평균, 정규는 그날 rate (계산 규칙 1)."""
        base = Decimal("21.50")
        reg, ot, _ = day_amounts(self._day(regular=480, ot=120), Decimal("24.00"), base)
        assert reg == Decimal("192")  # 8h × 그날 $24
        assert ot == Decimal("64.50")  # 2h × 1.5 × 가중평균 $21.50

    def test_returns_unrounded_values(self) -> None:
        """반올림 시점은 호출자 몫 — 여기선 자르지 않는다 (구간 누적 정확도)."""
        reg, _, _ = day_amounts(self._day(regular=370), _R20, _R20)
        assert reg == Decimal("20.00") * 370 / Decimal(60)
        assert reg != reg.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# frozen breakdown 계약 (calc_version=1)
# ---------------------------------------------------------------------------


class TestFrozenBreakdownContract:
    def _breakdown_dict(self, **overrides) -> dict:
        bd = EntryBreakdown(
            calc_version=CALC_VERSION,
            days=[
                DayDetail(
                    work_date=_MON, regular_minutes=480, ot_minutes=60,
                    dt_minutes=0, applied_rate=Decimal("20.00"),
                )
            ],
        )
        data = bd.model_dump(mode="json")
        data.update(overrides)
        return data

    def test_roundtrip_parse(self) -> None:
        """JSONB dict(모델 json 직렬화 형태) → 모델 복원 (날짜/Decimal 코션)."""
        parsed = parse_frozen_breakdown(self._breakdown_dict())
        assert parsed.calc_version == CALC_VERSION
        assert parsed.days[0].work_date == _MON
        assert parsed.days[0].applied_rate == Decimal("20.00")

    def test_version_mismatch_rejected(self) -> None:
        with pytest.raises(BadRequestError, match="calc_version"):
            parse_frozen_breakdown(self._breakdown_dict(calc_version=99))

    def test_unreadable_breakdown_rejected(self) -> None:
        with pytest.raises(BadRequestError, match="unreadable"):
            parse_frozen_breakdown({"calc_version": 1, "days": [{"bogus": True}]})

    def test_legacy_day_without_amounts_parses_as_none(self) -> None:
        """금액 필드가 생기기 전 동결본 — 재계산 없이 그대로 통과, 금액은 None.

        additive-compatible 이므로 calc_version 은 1 그대로여야 하고, 옛 entry 를
        되살려 계산하는 일이 없어야 한다 (표시 쪽이 None 을 다룬다).
        """
        legacy = {
            "calc_version": 1,
            "segments": [
                {
                    "rate": "20.00", "regular_minutes": 480, "ot_minutes": 60,
                    "dt_minutes": 0, "amount": "190.00",
                }
            ],
            "days": [
                {
                    "work_date": "2026-07-06", "regular_minutes": 480,
                    "ot_minutes": 60, "dt_minutes": 0, "applied_rate": "20.00",
                }
            ],
            "penalties": [],
        }
        parsed = parse_frozen_breakdown(legacy)
        day = parsed.days[0]
        assert (day.regular_minutes, day.ot_minutes) == (480, 60)
        assert day.applied_rate == Decimal("20.00")
        assert day.regular_amount is None
        assert day.ot_amount is None
        assert day.dt_amount is None
        assert day.total_amount is None
        assert parsed.segments[0].amount == Decimal("190.00")  # 동결 금액 그대로

    def test_legacy_breakdown_without_context_days_parses_as_empty(self) -> None:
        """context_days 가 없던 동결본 — 빈 목록으로 파싱 (calc_version 유지).

        "경계 걸친 주가 없었다"(빈 목록)와 "옛 포맷"(키 자체 없음)은 값으로는
        구분되지 않는다 — 구분이 필요한 백필 쪽은 키 유무를 본다.
        """
        legacy = self._breakdown_dict()
        legacy.pop("context_days", None)
        parsed = parse_frozen_breakdown(legacy)
        assert parsed.calc_version == CALC_VERSION
        assert parsed.context_days == []

    def test_context_days_roundtrip(self) -> None:
        """context_days 가 있으면 그대로 복원 (날짜/정수/플래그)."""
        bd = EntryBreakdown(
            calc_version=CALC_VERSION,
            context_days=[ContextDay(work_date=_SUN, net_minutes=1200)],
        )
        parsed = parse_frozen_breakdown(bd.model_dump(mode="json"))
        assert len(parsed.context_days) == 1
        context = parsed.context_days[0]
        assert context.work_date == _SUN
        assert context.net_minutes == 1200
        assert context.paid_in_prior is True

    def test_legacy_day_still_converts_for_reclassification(self) -> None:
        """옛 동결 일도 경계 걸친 주 입력(WeekDay)으로 그대로 쓰인다."""
        legacy_day = DayDetail(
            work_date=_MON, regular_minutes=480, ot_minutes=60,
            applied_rate=Decimal("20.00"),
        )
        assert legacy_day.total_amount is None
        wd = frozen_day_to_week_day(legacy_day)
        assert (wd.frozen, wd.net_minutes, wd.frozen_regular) == (True, 540, 480)

    def test_frozen_day_to_week_day(self) -> None:
        """DayDetail → WeekDay 변환: net = reg+ot+dt, frozen 성분 보존."""
        detail = DayDetail(
            work_date=_MON, regular_minutes=480, ot_minutes=120,
            dt_minutes=60, applied_rate=Decimal("21.00"),
        )
        wd = frozen_day_to_week_day(detail)
        assert wd.frozen is True
        assert wd.net_minutes == 660
        assert (wd.frozen_regular, wd.frozen_ot, wd.frozen_dt) == (480, 120, 60)
        assert wd.rate == Decimal("21.00")
