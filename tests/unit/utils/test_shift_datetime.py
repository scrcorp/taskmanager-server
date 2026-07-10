"""assemble_shift_datetimes / net_minutes_from_datetimes 단위 테스트.

스케줄 시간저장 벽시계 datetime 인코딩(start_at/end_at)의 조립·순근무 계산 검증.
"""
from datetime import date, datetime, time

from app.utils.timezone import assemble_shift_datetimes, net_minutes_from_datetimes


class TestAssembleShiftDatetimes:
    def test_same_day(self):
        s, e = assemble_shift_datetimes(date(2026, 7, 8), time(9, 0), time(17, 0))
        assert s == datetime(2026, 7, 8, 9, 0)
        assert e == datetime(2026, 7, 8, 17, 0)

    def test_overnight_rolls_end_to_next_day(self):
        # end <= start → end 다음날
        s, e = assemble_shift_datetimes(date(2026, 7, 8), time(22, 0), time(2, 0))
        assert s == datetime(2026, 7, 8, 22, 0)
        assert e == datetime(2026, 7, 9, 2, 0)

    def test_end_at_midnight(self):
        # 00:00 종료 = 다음날 자정 (end_time 00:00 <= start)
        s, e = assemble_shift_datetimes(date(2026, 7, 8), time(15, 30), time(0, 0))
        assert e == datetime(2026, 7, 9, 0, 0)

    def test_explicit_start_date_early_morning(self):
        # 영업일 7/8 이지만 실제 근무는 7/9 새벽 1시 (명시 날짜)
        s, e = assemble_shift_datetimes(
            date(2026, 7, 8), time(1, 0), time(9, 0), start_date=date(2026, 7, 9)
        )
        assert s == datetime(2026, 7, 9, 1, 0)
        assert e == datetime(2026, 7, 9, 9, 0)

    def test_explicit_end_date_overrides_auto_roll(self):
        s, e = assemble_shift_datetimes(
            date(2026, 7, 8), time(9, 0), time(10, 0), end_date=date(2026, 7, 10)
        )
        assert e == datetime(2026, 7, 10, 10, 0)

    def test_none_times(self):
        s, e = assemble_shift_datetimes(date(2026, 7, 8), None, None)
        assert s is None and e is None

    def test_start_only(self):
        s, e = assemble_shift_datetimes(date(2026, 7, 8), time(9, 0), None)
        assert s == datetime(2026, 7, 8, 9, 0)
        assert e is None


class TestNetMinutesFromDatetimes:
    def test_basic(self):
        assert net_minutes_from_datetimes(
            datetime(2026, 7, 8, 9, 0), datetime(2026, 7, 8, 17, 0)
        ) == 480

    def test_overnight_no_special_casing(self):
        assert net_minutes_from_datetimes(
            datetime(2026, 7, 8, 22, 0), datetime(2026, 7, 9, 2, 0)
        ) == 240

    def test_with_break(self):
        assert net_minutes_from_datetimes(
            datetime(2026, 7, 8, 9, 0),
            datetime(2026, 7, 8, 17, 0),
            datetime(2026, 7, 8, 12, 0),
            datetime(2026, 7, 8, 12, 30),
        ) == 450

    def test_none_returns_zero(self):
        assert net_minutes_from_datetimes(None, datetime(2026, 7, 8, 17, 0)) == 0
        assert net_minutes_from_datetimes(datetime(2026, 7, 8, 9, 0), None) == 0

    def test_negative_clamped_to_zero(self):
        assert net_minutes_from_datetimes(
            datetime(2026, 7, 8, 17, 0), datetime(2026, 7, 8, 9, 0)
        ) == 0
