"""Unit tests for 스케줄 시간 grid 강제 (D6: 5분 단일 단위).

정책:
  - 입력 단위는 `SCHEDULE_STEP_MINUTES`(5) **하나뿐**이다. 경로별 차등(console 30 / kiosk 5) 폐기.
  - grid 판정의 **단일 관문**은 `schedule_service._normalize_shift_input` 이다.
    스키마(ScheduleCreate/Update, ManageSchedule*Request)는 grid 를 판정하지 않는다 —
    중복 검증하면 같은 위반이 400 과 422 두 형태로 나가고, 워크인처럼 이미 저장된
    비배수 값이 실린 요청을 통째로 막는다(D7).

분기 전수 커버:
  - validate_grid: None / "" / valid / off-grid 분 / 잘못된 포맷
  - 관문: 구(HH:MM) 인코딩 / 신(ISO) 인코딩 / 휴게 / 클라 전송분 한정
  - 스키마 3종이 grid 를 판정하지 않음
  - bulk_upload_service._is_off_grid 헬퍼
"""

from datetime import date, datetime
from uuid import uuid4

import pytest

from app.schemas.attendance_device import (
    ManageScheduleCreateRequest,
    ManageScheduleUpdateRequest,
)
from app.schemas.schedule import (
    SCHEDULE_STEP_MINUTES,
    ScheduleCreate,
    ScheduleUpdate,
    grid_error_message,
    validate_grid,
)
from app.services.bulk_upload_service import bulk_upload_service
from app.services.schedule_service import schedule_service
from app.utils.exceptions import BadRequestError


class TestStepConstant:
    def test_single_unit_is_five(self):
        assert SCHEDULE_STEP_MINUTES == 5

    def test_no_kiosk_specific_constant_remains(self):
        """경로별 차등 상수가 되살아나면 즉시 실패시킨다."""
        import app.schemas.schedule as sched_schemas

        assert not hasattr(sched_schemas, "KIOSK_STEP_MINUTES")


class TestValidateGrid:
    @pytest.mark.parametrize("value", [None, "", "00:00", "09:05", "10:15", "23:55", "12:30"])
    def test_passes(self, value):
        assert validate_grid(value) == value

    @pytest.mark.parametrize("value", ["09:17", "09:01", "23:59", "00:07"])
    def test_off_grid_minute_rejects(self, value):
        with pytest.raises(ValueError, match="5-minute increments"):
            validate_grid(value)

    @pytest.mark.parametrize("value", ["9:30", "0930", "24:00", "12:60", "abc"])
    def test_bad_format_rejects(self, value):
        with pytest.raises(ValueError, match="HH:MM"):
            validate_grid(value)

    def test_error_message_states_the_step(self):
        assert grid_error_message() == "Time must be in 5-minute increments."


# ── 스키마는 grid 를 판정하지 않는다 ──────────────────────────────


def _create(**over):
    base = dict(
        user_id="u", store_id="s", work_date="2026-06-16",
        start_time="09:00", end_time="17:00",
    )
    base.update(over)
    return ScheduleCreate(**base)


class TestSchemasDoNotJudgeGrid:
    """관문이 하나여야 하므로 스키마는 통과시킨다."""

    def test_schedule_create_passes_off_grid(self):
        assert _create(start_time="09:17").start_time == "09:17"

    def test_schedule_update_passes_off_grid(self):
        assert ScheduleUpdate(end_time="10:07").end_time == "10:07"

    def test_manage_create_passes_off_grid(self):
        """키오스크 스키마의 422 관문 제거 확인 — 판정은 서비스에서만."""
        req = ManageScheduleCreateRequest(
            user_id=uuid4(), start_time="10:17", end_time="14:00",
        )
        assert req.start_time == "10:17"

    def test_manage_update_passes_off_grid(self):
        assert ManageScheduleUpdateRequest(start_time="11:07").start_time == "11:07"

    def test_manage_create_still_accepts_on_grid(self):
        req = ManageScheduleCreateRequest(
            user_id=uuid4(), start_time="10:15", end_time="14:45",
        )
        assert req.start_time == "10:15"


# ── 단일 관문 ────────────────────────────────────────────────


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
    def test_on_grid_passes(self):
        assert _norm()["start_at"].hour == 9

    def test_five_minute_values_pass_on_every_path(self):
        """예전엔 console 경로가 30분이라 :15 가 거부됐다. 이제 통과해야 한다."""
        norm = _norm(start_time="09:15", end_time="17:45")
        assert norm["start_at"].minute == 15
        assert norm["end_at"].minute == 45

    @pytest.mark.parametrize("field,value", [
        ("start_time", "09:17"), ("end_time", "17:43"),
    ])
    def test_off_grid_rejects(self, field, value):
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(**{field: value})

    def test_break_off_grid_rejects(self):
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(break_start_time="12:12", break_end_time="12:40")

    def test_break_on_grid_passes(self):
        norm = _norm(break_start_time="12:15", break_end_time="12:45")
        assert norm["break_start_at"].minute == 15

    def test_iso_encoding_off_grid_rejects(self):
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(start_time=None, end_time=None,
                  start_at="2026-06-16T09:17", end_at="2026-06-16T17:00")

    def test_iso_encoding_on_grid_passes(self):
        norm = _norm(start_time=None, end_time=None,
                     start_at="2026-06-16T09:15", end_at="2026-06-16T17:45")
        assert norm["start_at"].minute == 15

    def test_seconds_are_rejected(self):
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(start_time=None, end_time=None,
                  start_at="2026-06-16T09:15:30", end_at="2026-06-16T17:00")

class TestChangedValueOnly:
    """D7-3 — 검사 대상은 '이번에 값이 바뀐 필드'뿐이다.

    워크인처럼 이미 저장된 비그리드 값(09:07)은 그대로면 면제된다.
    이 규칙 덕분에 클라이언트가 **항상 전체를 보내도** 안전하다.
    """

    def _prev(self, start="09:07", end="17:07"):
        """entry 에 저장돼 있던 비그리드 시각."""
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        return {
            "start_at": datetime(2026, 6, 16, sh, sm),
            "end_at": datetime(2026, 6, 16, eh, em),
            "break_start_at": None,
            "break_end_at": None,
        }

    def test_unchanged_off_grid_value_is_exempt(self):
        """워크인 09:07 을 그대로 두고 다른 값만 고치면 통과해야 한다."""
        norm = _norm(start_time="09:07", end_time="18:00", prev_at=self._prev())
        assert norm["start_at"].minute == 7   # 캐리값 보존
        assert norm["end_at"].hour == 18

    def test_changed_value_is_checked(self):
        """같은 필드라도 값을 바꾸면 그때는 5분 단위가 적용된다(D7-4)."""
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(start_time="09:12", end_time="17:07", prev_at=self._prev())

    def test_full_send_of_unchanged_values_is_safe(self):
        """전체 전송해도 안 바뀐 값은 전부 면제 — 부분 전송이 불필요해진다."""
        norm = _norm(start_time="09:07", end_time="17:07", prev_at=self._prev())
        assert norm["start_at"].minute == 7
        assert norm["end_at"].minute == 7

    def test_create_path_checks_everything(self):
        """prev_at 이 없으면 신규 생성 — 모든 값이 새 값이므로 전부 검사."""
        with pytest.raises(BadRequestError, match="5-minute increments"):
            _norm(start_time="09:07", end_time="17:00")

    def test_break_unchanged_is_exempt(self):
        prev = self._prev(start="09:00", end="17:00")
        prev["break_start_at"] = datetime(2026, 6, 16, 12, 7)
        prev["break_end_at"] = datetime(2026, 6, 16, 12, 37)
        norm = _norm(start_time="10:00", end_time="17:00",
                     break_start_time="12:07", break_end_time="12:37",
                     prev_at=prev)
        assert norm["break_start_at"].minute == 7


class TestBulkOffGridHelper:
    def test_detects_off_grid(self):
        assert bulk_upload_service._is_off_grid("09:17", "17:00") is True

    def test_five_minute_values_now_pass(self):
        """벌크도 같은 단위를 쓴다 — 예전엔 30분이라 :15 가 걸렸다."""
        assert bulk_upload_service._is_off_grid("09:15", "17:45") is False

    def test_all_on_grid_with_none(self):
        assert bulk_upload_service._is_off_grid("09:30", "17:00", None) is False

    def test_break_off_grid(self):
        assert bulk_upload_service._is_off_grid("09:00", "17:00", "12:12", "12:40") is True
