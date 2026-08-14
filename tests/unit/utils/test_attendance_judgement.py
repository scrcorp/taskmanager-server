"""근태 판정 순수 함수(judge_clock_in / judge_clock_out) 단위 테스트.

확정 스펙 (스케줄 17:00~21:00, late_buffer=5, early_clock_in=10, early_leave=10):
    - clock-in  16:50 정상 / 16:49 부터 early(사유 요구)
    - clock-in  17:05:59 정상 / 17:06:00 부터 late
    - clock-out 20:50 정상 / 20:49 부터 early leave

판정은 **분 단위**다. 기록(clock_in/out)은 초까지 저장하지만 비교 전에 절삭한다 —
화면이 HH:MM 을 보여주는데 기록만 초 때문에 지각으로 찍히면 설명할 수 없다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.utils.attendance_judgement import (
    DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES,
    DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES,
    DEFAULT_LATE_BUFFER_MINUTES,
    ClockInKind,
    ClockOutKind,
    is_late_arrival,
    judge_clock_in,
    judge_clock_out,
)

UTC = timezone.utc

# 기준 스케줄 — 2026-08-13 17:00 ~ 21:00 (UTC 로 단순화)
START = datetime(2026, 8, 13, 17, 0, tzinfo=UTC)
END = datetime(2026, 8, 13, 21, 0, tzinfo=UTC)

LATE_BUFFER = 5
EARLY_IN = 10
EARLY_OUT = 10


def at(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 8, 13, hour, minute, second, micro, tzinfo=UTC)


def _in(value: datetime):
    return judge_clock_in(
        value, START, late_buffer=LATE_BUFFER, early_threshold=EARLY_IN
    )


def _out(value: datetime):
    return judge_clock_out(value, END, early_leave_threshold=EARLY_OUT)


class TestDefaultsMatchSeed:
    """코드 fallback 기본값 — settings_seed 의 default_value 와 같아야 한다.

    갈리면 설정이 등록된 매장과 아닌 매장이 조용히 다르게 동작한다.
    """

    def test_defaults_match_settings_seed(self):
        from app.seeds.settings_seed import SETTINGS_SEED

        by_key = {d.key: d.default_value for d in SETTINGS_SEED}
        assert by_key["attendance.late_buffer_minutes"] == DEFAULT_LATE_BUFFER_MINUTES
        assert (
            by_key["attendance.early_clock_in_threshold_minutes"]
            == DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES
        )
        assert (
            by_key["attendance.early_leave_threshold_minutes"]
            == DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES
        )

    def test_spec_values(self):
        """확정 스펙: early 계열 10분, late buffer 5분."""
        assert DEFAULT_LATE_BUFFER_MINUTES == 5
        assert DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES == 10
        assert DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES == 10


class TestClockInEarlyBoundary:
    """16:50 정상 / 16:49 early — 경계는 "임계값 분 전 정각"까지 정상."""

    def test_exactly_threshold_before_is_on_time(self):
        v = _in(at(16, 50))
        assert v.kind is ClockInKind.ON_TIME
        assert not v.is_early
        assert v.minutes_early == 10

    def test_one_minute_earlier_is_early(self):
        v = _in(at(16, 49))
        assert v.kind is ClockInKind.EARLY
        assert v.is_early
        assert v.minutes_early == 11

    def test_far_early_has_no_upper_limit(self):
        """상한 없음 — 몇 시간 전이어도 '거부'가 아니라 early 판정일 뿐."""
        v = _in(at(9, 0))
        assert v.kind is ClockInKind.EARLY
        assert v.minutes_early == 480

    def test_early_boundary_seconds_are_dropped(self):
        """16:49:59 는 화면상 16:49 → early. 16:50:59 는 16:50 → 정상."""
        assert _in(at(16, 49, 59, 999999)).kind is ClockInKind.EARLY
        assert _in(at(16, 50, 59, 999999)).kind is ClockInKind.ON_TIME


class TestClockInLateBoundary:
    """17:05:59 정상 / 17:06:00 부터 late."""

    def test_on_the_dot_is_on_time(self):
        v = _in(at(17, 0))
        assert v.kind is ClockInKind.ON_TIME
        assert v.minutes_early == 0
        assert v.minutes_late == 0

    def test_exactly_buffer_is_on_time(self):
        assert _in(at(17, 5)).kind is ClockInKind.ON_TIME

    def test_last_second_of_buffer_minute_is_on_time(self):
        """17:05:59 — 초를 살리면 지각이 되는 지점. 절삭 규칙의 핵심 케이스."""
        v = _in(at(17, 5, 59, 999999))
        assert v.kind is ClockInKind.ON_TIME
        assert v.minutes_late == 5

    def test_one_minute_past_buffer_is_late(self):
        v = _in(at(17, 6))
        assert v.kind is ClockInKind.LATE
        assert v.is_late
        assert v.minutes_late == 6

    def test_late_minutes_drop_seconds(self):
        """17:06:59 는 화면상 17:06 → 6분 지각 (반올림 7분 아님)."""
        assert _in(at(17, 6, 59)).minutes_late == 6

    def test_zero_buffer_makes_next_minute_late(self):
        v = judge_clock_in(at(17, 1), START, late_buffer=0, early_threshold=EARLY_IN)
        assert v.kind is ClockInKind.LATE
        # buffer 0 이어도 같은 분(초만 지남)은 지각이 아니다.
        assert (
            judge_clock_in(
                at(17, 0, 59), START, late_buffer=0, early_threshold=EARLY_IN
            ).kind
            is ClockInKind.ON_TIME
        )


class TestClockInEdgeCases:
    def test_no_schedule_start_is_unknown(self):
        v = judge_clock_in(at(17, 0), None, late_buffer=5, early_threshold=10)
        assert v.kind is ClockInKind.UNKNOWN
        assert v.minutes_early == 0 and v.minutes_late == 0

    def test_no_clock_time_is_unknown(self):
        assert (
            judge_clock_in(None, START, late_buffer=5, early_threshold=10).kind
            is ClockInKind.UNKNOWN
        )

    def test_early_wins_over_late_and_they_are_exclusive(self):
        """early 와 late 는 배타 — 라벨이 서로를 덮어쓰지 않는다."""
        v = _in(at(10, 0))
        assert v.is_early and not v.is_late

    def test_negative_threshold_is_clamped_to_zero(self):
        """잘못된 설정(-5)이 들어와도 판정이 뒤집히지 않는다."""
        v = judge_clock_in(at(17, 0), START, late_buffer=-5, early_threshold=-5)
        assert v.kind is ClockInKind.ON_TIME

    def test_none_thresholds_behave_as_zero(self):
        v = judge_clock_in(at(17, 1), START, late_buffer=None, early_threshold=None)
        assert v.kind is ClockInKind.LATE

    def test_cross_timezone_comparison(self):
        """서로 다른 tz 의 aware 시각이라도 절대시각으로 비교된다."""
        from zoneinfo import ZoneInfo

        la_start = datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # 10:00 LA = 17:00 UTC
        v = judge_clock_in(
            at(17, 5), la_start, late_buffer=LATE_BUFFER, early_threshold=EARLY_IN
        )
        assert v.kind is ClockInKind.ON_TIME


class TestClockOutEarlyBoundary:
    """20:50 정상 / 20:49 early leave."""

    def test_exactly_threshold_before_end_is_on_time(self):
        v = _out(at(20, 50))
        assert v.kind is ClockOutKind.ON_TIME
        assert not v.is_early
        assert v.minutes_early == 10

    def test_one_minute_earlier_is_early(self):
        v = _out(at(20, 49))
        assert v.kind is ClockOutKind.EARLY
        assert v.is_early
        assert v.minutes_early == 11

    def test_boundary_seconds_are_dropped(self):
        """20:49:59 는 화면상 20:49 → 조기. 20:50:30 은 20:50 → 정상."""
        assert _out(at(20, 49, 59, 999999)).kind is ClockOutKind.EARLY
        assert _out(at(20, 50, 30)).kind is ClockOutKind.ON_TIME

    def test_on_time_at_end(self):
        v = _out(at(21, 0))
        assert v.kind is ClockOutKind.ON_TIME
        assert v.minutes_early == 0

    def test_after_end_is_on_time_not_negative(self):
        v = _out(at(22, 30))
        assert v.kind is ClockOutKind.ON_TIME
        assert v.minutes_early == 0

    @pytest.mark.parametrize("missing", ["at", "end"])
    def test_missing_time_is_unknown(self, missing: str):
        value = None if missing == "at" else at(20, 0)
        end = None if missing == "end" else END
        assert (
            judge_clock_out(value, end, early_leave_threshold=10).kind
            is ClockOutKind.UNKNOWN
        )


class TestIsLateArrival:
    """미출근 승격 전용 헬퍼 — early 개념 없이 late 만 본다."""

    def test_before_start_is_not_late(self):
        assert is_late_arrival(at(9, 0), START, late_buffer=LATE_BUFFER) is False

    def test_within_buffer_is_not_late(self):
        assert is_late_arrival(at(17, 5, 59), START, late_buffer=LATE_BUFFER) is False

    def test_past_buffer_is_late(self):
        assert is_late_arrival(at(17, 6), START, late_buffer=LATE_BUFFER) is True

    def test_unknown_schedule_is_not_late(self):
        assert is_late_arrival(at(17, 6), None, late_buffer=LATE_BUFFER) is False
