"""roster 일간 컬럼의 +1d 새벽 물리 배치 — offset 순수함수 단위 테스트."""
from datetime import date, datetime, time

from app.services.schedule_service import ScheduleService


class _S:
    def __init__(self, operating_day=None, work_date=None, start_at=None):
        self.operating_day = operating_day
        self.work_date = work_date
        self.start_at = start_at


class TestStartOffsetDays:
    def test_same_day(self):
        s = _S(date(2026, 7, 24), date(2026, 7, 24), datetime(2026, 7, 24, 9))
        assert ScheduleService._start_offset_days(s) == 0

    def test_plus_one_day_dawn(self):
        s = _S(date(2026, 7, 24), date(2026, 7, 24), datetime(2026, 7, 25, 1))
        assert ScheduleService._start_offset_days(s) == 1

    def test_start_at_none(self):
        s = _S(date(2026, 7, 24), date(2026, 7, 24), None)
        assert ScheduleService._start_offset_days(s) == 0

    def test_operating_day_none_falls_back_to_work_date(self):
        s = _S(None, date(2026, 7, 24), datetime(2026, 7, 25, 1))
        assert ScheduleService._start_offset_days(s) == 1

    def test_clamped_to_0_1(self):
        neg = _S(date(2026, 7, 24), date(2026, 7, 24), datetime(2026, 7, 23, 9))
        big = _S(date(2026, 7, 24), date(2026, 7, 24), datetime(2026, 7, 27, 9))
        assert ScheduleService._start_offset_days(neg) == 0
        assert ScheduleService._start_offset_days(big) == 1


class TestHourOccupancyOffset:
    def test_offset_places_on_next_day_axis(self):
        # +1d 01:00~09:00 → 영업일 축 25..33
        assert ScheduleService._hour_occupancy(time(1), time(9), 25, offset_days=1) == 1.0
        assert ScheduleService._hour_occupancy(time(1), time(9), 32, offset_days=1) == 1.0
        assert ScheduleService._hour_occupancy(time(1), time(9), 33, offset_days=1) == 0.0

    def test_offset_vacates_same_day_morning(self):
        assert ScheduleService._hour_occupancy(time(1), time(9), 1, offset_days=1) == 0.0

    def test_half_hour_with_offset(self):
        assert ScheduleService._hour_occupancy(time(0, 30), time(2), 24, offset_days=1) == 0.5

    def test_default_offset_backward_compatible(self):
        assert ScheduleService._hour_occupancy(time(22), time(2), 25) == 1.0
        assert ScheduleService._hour_occupancy(time(9), time(17), 9) == 1.0


class TestOccupiesSlotOffset:
    def test_offset_slot(self):
        assert ScheduleService._occupies_slot(time(1), time(9), 25.0, offset_days=1) is True
        assert ScheduleService._occupies_slot(time(1), time(9), 24.5, offset_days=1) is False
        assert ScheduleService._occupies_slot(time(1), time(9), 1.0, offset_days=1) is False

    def test_default_offset_backward_compatible(self):
        assert ScheduleService._occupies_slot(time(22), time(2), 25.5) is True
