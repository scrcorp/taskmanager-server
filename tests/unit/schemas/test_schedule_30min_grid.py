"""Unit tests for 스케줄 시간 grid 강제.

grid 판정은 schedule_service._normalize_shift_input(step_minutes) 단일 관문에 있다.
ScheduleCreate/ScheduleUpdate 는 step 을 모르므로 스키마 검증을 하지 않는다.

분기 전수 커버:
  - validate_30min_grid / validate_kiosk_grid: None / "" / valid / off-grid 분 / 잘못된 포맷
  - 관문: 구(HH:MM) 인코딩 / 신(ISO) 인코딩 × step 30·5 × 클라 전송분만 검사
  - 키오스크 edge 스키마(ManageSchedule*Request): 5분 허용, 그 외 reject
  - bulk_upload_service._is_off_30min_grid 헬퍼
"""

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.attendance_device import (
    ManageScheduleCreateRequest,
    ManageScheduleUpdateRequest,
)
from app.schemas.schedule import (
    ScheduleCreate,
    ScheduleUpdate,
    grid_error_message,
    validate_30min_grid,
    validate_kiosk_grid,
)
from app.services.bulk_upload_service import bulk_upload_service
from app.services.schedule_service import schedule_service
from app.utils.exceptions import BadRequestError


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

    def test_schema_does_not_judge_grid(self):
        """스키마는 step 을 모르므로 통과시킨다 — 판정은 관문(_normalize_shift_input)."""
        assert _create(start_time="09:17").start_time == "09:17"

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

    def test_schema_does_not_judge_grid(self):
        assert ScheduleUpdate(end_time="10:05").end_time == "10:05"


def _norm(**over):
    """관문 호출 헬퍼 — 구 인코딩 기본."""
    base = dict(
        work_date=date(2026, 6, 16), operating_day=date(2026, 6, 16),
        start_time="09:00", end_time="17:00",
        break_start_time=None, break_end_time=None,
        start_at=None, end_at=None, break_start_at=None, break_end_at=None,
    )
    base.update(over)
    return schedule_service._normalize_shift_input(**base)


class TestGridGate:
    """grid 판정 단일 관문 — 구/신 인코딩, step 30/5, 클라 전송분 한정."""

    def test_legacy_on_grid_passes(self):
        assert _norm()["start_at"].hour == 9

    @pytest.mark.parametrize("field,value", [
        ("start_time", "09:17"), ("end_time", "17:45"),
    ])
    def test_legacy_off_grid_rejects_at_default_step(self, field, value):
        with pytest.raises(BadRequestError, match="hour or half-hour"):
            _norm(**{field: value})

    def test_legacy_break_off_grid_rejects(self):
        with pytest.raises(BadRequestError):
            _norm(break_start_time="12:10", break_end_time="12:40")

    def test_kiosk_step_allows_5min(self):
        norm = _norm(start_time="09:15", end_time="17:45", step_minutes=5)
        assert norm["start_at"].minute == 15
        assert norm["end_at"].minute == 45

    def test_kiosk_step_still_rejects_off_5min(self):
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(start_time="09:17", step_minutes=5)

    def test_iso_encoding_off_grid_rejects(self):
        with pytest.raises(BadRequestError):
            _norm(start_time=None, end_time=None,
                  start_at="2026-06-16T09:17", end_at="2026-06-16T17:00")

    def test_iso_encoding_5min_passes_at_kiosk_step(self):
        norm = _norm(start_time=None, end_time=None,
                     start_at="2026-06-16T09:15", end_at="2026-06-16T17:45",
                     step_minutes=5)
        assert norm["start_at"].minute == 15

    def test_only_client_sent_legacy_fields_are_checked(self):
        """캐리된 비그리드 값(워크인 09:07 등)은 수정 자체를 막지 않는다."""
        norm = _norm(start_time="09:30", end_time="17:07",
                     client_time_fields={"start_time"})
        assert norm["end_at"].minute == 7  # 캐리값은 통과

    def test_client_sent_field_is_checked_even_when_others_carry(self):
        with pytest.raises(BadRequestError):
            _norm(start_time="09:07", end_time="17:07",
                  client_time_fields={"start_time"})


class TestKioskGrid:
    """키오스크(HTMA)는 5분 step — console 30분 규칙과 독립이어야 한다."""

    @pytest.mark.parametrize("value", [None, "", "00:00", "09:05", "10:15", "23:55"])
    def test_passes(self, value):
        assert validate_kiosk_grid(value) == value

    @pytest.mark.parametrize("value", ["09:17", "09:01", "23:59"])
    def test_off_grid_minute_rejects(self, value):
        with pytest.raises(ValueError, match="5-minute increments"):
            validate_kiosk_grid(value)

    def test_manage_create_request_accepts_5min(self):
        req = ManageScheduleCreateRequest(
            user_id=uuid4(), start_time="10:15", end_time="14:45",
        )
        assert req.start_time == "10:15"

    def test_manage_create_request_rejects_off_5min(self):
        with pytest.raises(ValidationError):
            ManageScheduleCreateRequest(
                user_id=uuid4(), start_time="10:17", end_time="14:00",
            )

    def test_manage_update_request_accepts_5min(self):
        assert ManageScheduleUpdateRequest(start_time="11:35").start_time == "11:35"

    def test_manage_update_request_rejects_off_5min(self):
        with pytest.raises(ValidationError):
            ManageScheduleUpdateRequest(start_time="11:07")

    def test_console_still_30min(self):
        """키오스크 완화가 console 로 새지 않았는지 — 같은 값이 기본 step 관문에선 거부."""
        with pytest.raises(BadRequestError, match="hour or half-hour"):
            _norm(start_time="10:15")


class TestGridErrorMessage:
    def test_30_keeps_existing_wording(self):
        assert grid_error_message(30) == "Time must be on the hour or half-hour (:00 or :30)."

    def test_other_steps_state_the_step(self):
        assert grid_error_message(5) == "Time must be in 5-minute increments."
        assert grid_error_message(15) == "Time must be in 15-minute increments."


class TestBulkOffGridHelper:
    def test_detects_off_grid(self):
        assert bulk_upload_service._is_off_30min_grid("09:15", "17:00") is True

    def test_all_on_grid_with_none(self):
        assert bulk_upload_service._is_off_30min_grid("09:30", "17:00", None) is False

    def test_break_off_grid(self):
        assert bulk_upload_service._is_off_30min_grid("09:00", "17:00", "12:10", "12:40") is True
