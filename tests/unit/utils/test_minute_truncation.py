"""분 단위 절삭 규칙(R1/R2) 헬퍼 테스트.

규칙: 기록은 초까지 보존하되, 표시·계산은 각 타임스탬프를 분으로 절삭한 뒤 수행한다.
"차이를 내림" 이 아니라 "절삭 후 차이" 라는 점이 핵심 — 화면의 HH:MM 끼리의
뺄셈과 지표 값이 항상 일치해야 한다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils.timezone import (
    floor_to_minute,
    minutes_between,
    net_minutes_from_datetimes,
)

UTC = timezone.utc


def dt(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 8, 8, hour, minute, second, micro, tzinfo=UTC)


class TestFloorToMinute:
    def test_drops_seconds_and_micros(self):
        assert floor_to_minute(dt(18, 1, 59, 999999)) == dt(18, 1)

    def test_keeps_exact_minute(self):
        assert floor_to_minute(dt(18, 1)) == dt(18, 1)

    def test_none_passthrough(self):
        assert floor_to_minute(None) is None


class TestMinutesBetween:
    def test_truncate_then_subtract_not_floor_of_diff(self):
        """22:26:50 → 22:57:20 은 실제 30분 30초지만 화면엔 22:26–22:57 로 보인다.

        차이를 내림하면 30, 절삭 후 차이는 31 — 후자가 규칙.
        """
        assert minutes_between(dt(22, 26, 50), dt(22, 57, 20)) == 31

    def test_late_case_no_rounding_up(self):
        """clock_in 18:01:40 vs sched 17:30 → 표시(18:01)와 같은 31분. round 면 32."""
        assert minutes_between(dt(17, 30), dt(18, 1, 40)) == 31

    def test_exact_minutes(self):
        assert minutes_between(dt(18, 0), dt(23, 3)) == 303

    def test_same_minute_different_seconds_is_zero(self):
        assert minutes_between(dt(18, 1, 5), dt(18, 1, 55)) == 0

    def test_negative_clamped_to_zero(self):
        assert minutes_between(dt(18, 0), dt(17, 0)) == 0

    @pytest.mark.parametrize(
        "start,end",
        [(None, dt(18, 0)), (dt(18, 0), None), (None, None)],
    )
    def test_none_returns_zero(self, start, end):
        assert minutes_between(start, end) == 0

    def test_crosses_midnight(self):
        start = dt(22, 30, 40)
        end = start + timedelta(hours=6)
        assert minutes_between(start, end) == 360

    def test_tz_aware_across_offsets(self):
        """서로 다른 오프셋이어도 동일 instant 기준으로 분 차이가 나온다."""
        kst = timezone(timedelta(hours=9))
        start = datetime(2026, 8, 8, 18, 1, 40, tzinfo=UTC)
        end = datetime(2026, 8, 9, 3, 4, 10, tzinfo=kst)  # = 18:04:10 UTC
        assert minutes_between(start, end) == 3


class TestNetMinutesFromDatetimes:
    def test_uses_truncated_minutes(self):
        # 총 303분 - break 31분
        total = net_minutes_from_datetimes(
            dt(18, 1, 40),
            dt(23, 4, 10),
            dt(22, 26, 50),
            dt(22, 57, 20),
        )
        assert total == 303 - 31

    def test_missing_bounds_returns_zero(self):
        assert net_minutes_from_datetimes(None, dt(23, 0)) == 0
