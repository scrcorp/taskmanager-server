"""Unit tests for 스케줄 시간 30분 grid 강제 (app.schemas.schedule).

분기 전수 커버:
  - validate_30min_grid: None / "" / valid(:00,:30) / off-grid 분 / 잘못된 포맷
  - ScheduleCreate: valid 통과, off-grid start/end/break reject
  - ScheduleUpdate: optional 필드 None 통과, off-grid reject
  - bulk_upload_service._is_off_30min_grid 헬퍼
"""

import pytest
from pydantic import ValidationError

from app.schemas.schedule import ScheduleCreate, ScheduleUpdate, validate_30min_grid
from app.services.bulk_upload_service import bulk_upload_service


class TestValidate30minGrid:
    @pytest.mark.parametrize("value", [None, "", "00:00", "09:30", "23:30", "12:00"])
    def test_passes(self, value):
        assert validate_30min_grid(value) == value

    @pytest.mark.parametrize("value", ["09:17", "09:15", "09:01", "00:45", "23:31"])
    def test_off_grid_minute_rejects(self, value):
        with pytest.raises(ValueError, match="hour or half-hour"):
            validate_30min_grid(value)

    @pytest.mark.parametrize("value", ["9:30", "0930", "24:00", "12:60", "abc"])
    def test_bad_format_rejects(self, value):
        with pytest.raises(ValueError):
            validate_30min_grid(value)


def _create(**over):
    base = dict(
        user_id="u", store_id="s", work_date="2026-06-16",
        start_time="09:00", end_time="17:00",
    )
    base.update(over)
    return ScheduleCreate(**base)


class TestScheduleCreate:
    def test_valid_passes(self):
        s = _create(start_time="09:30", end_time="17:30")
        assert s.start_time == "09:30"

    def test_valid_with_break_passes(self):
        s = _create(break_start_time="12:00", break_end_time="12:30")
        assert s.break_start_time == "12:00"

    def test_off_grid_start_rejects(self):
        with pytest.raises(ValidationError):
            _create(start_time="09:17")

    def test_off_grid_end_rejects(self):
        with pytest.raises(ValidationError):
            _create(end_time="17:45")

    def test_off_grid_break_rejects(self):
        with pytest.raises(ValidationError):
            _create(break_start_time="12:10", break_end_time="12:40")

    def test_none_break_passes(self):
        s = _create(break_start_time=None, break_end_time=None)
        assert s.break_start_time is None


class TestScheduleUpdate:
    def test_all_none_passes(self):
        u = ScheduleUpdate()
        assert u.start_time is None

    def test_valid_partial_passes(self):
        u = ScheduleUpdate(start_time="10:30")
        assert u.start_time == "10:30"

    def test_off_grid_rejects(self):
        with pytest.raises(ValidationError):
            ScheduleUpdate(end_time="10:05")


class TestBulkOffGridHelper:
    def test_detects_off_grid(self):
        assert bulk_upload_service._is_off_30min_grid("09:15", "17:00") is True

    def test_all_on_grid_with_none(self):
        assert bulk_upload_service._is_off_30min_grid("09:30", "17:00", None) is False

    def test_break_off_grid(self):
        assert bulk_upload_service._is_off_30min_grid("09:00", "17:00", "12:10", "12:40") is True
