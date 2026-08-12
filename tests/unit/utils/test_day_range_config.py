"""Unit tests — 영업시간/근무가능시간 값 형태 파서 (D2-7 / D2-8).

이 파서에 회귀 안전망이 **전혀 없었다.** 값 형태가 세 클라이언트 계약이고
클라마다 보정 코드가 갈라졌던 지점이므로, 형태를 고정하는 테스트를 여기 둔다.

분기 전수 커버:
  - `HH:MM` 파싱: 정상 / 24 이상 거부 / 분 범위 / 쓰레기 값
  - mode=all / mode=per_day / per_day 요일 누락 → all 폴백
  - closed 배열 (휴무는 closed 로만 표현)
  - end_offset_days: 0 / 1 / 없음 / 음수 / bool
  - 길이 0 (11:00–11:00 offset 0) 과 24시간 (offset 1) 구분
"""

from datetime import time

import pytest

from app.utils.timezone import (
    MINUTES_PER_DAY,
    is_closed_weekday,
    parse_wall_clock_minutes,
    resolve_day_range,
    store_day_start_from_org,
)

MON, TUE, SUN = 0, 1, 6


class TestParseWallClockMinutes:
    def test_normal(self):
        assert parse_wall_clock_minutes("00:00") == 0
        assert parse_wall_clock_minutes("06:30") == 390
        assert parse_wall_clock_minutes("23:59") == 1439

    def test_rejects_24_plus_notation(self):
        # 폐기된 표기(D2-8). 관용적으로 받아주면 되살아난다.
        assert parse_wall_clock_minutes("24:00") is None
        assert parse_wall_clock_minutes("26:00") is None

    def test_rejects_bad_minutes(self):
        assert parse_wall_clock_minutes("10:60") is None
        assert parse_wall_clock_minutes("-1:00") is None

    def test_rejects_garbage(self):
        assert parse_wall_clock_minutes(None) is None
        assert parse_wall_clock_minutes("") is None
        assert parse_wall_clock_minutes("noon") is None
        assert parse_wall_clock_minutes(600) is None


class TestResolveDayRangeAllMode:
    def test_simple(self):
        value = {"mode": "all", "all": {"start": "06:00", "end": "23:00", "end_offset_days": 0}}
        assert resolve_day_range(value, MON) == (360, 1380)

    def test_crosses_midnight_via_offset(self):
        value = {"mode": "all", "all": {"start": "11:00", "end": "02:00", "end_offset_days": 1}}
        assert resolve_day_range(value, MON) == (660, 120 + MINUTES_PER_DAY)

    def test_missing_offset_defaults_to_zero(self):
        value = {"mode": "all", "all": {"start": "06:00", "end": "23:00"}}
        assert resolve_day_range(value, MON) == (360, 1380)

    def test_no_implicit_next_day_correction(self):
        # 종료 < 시작인데 오프셋이 없으면 **보정하지 않는다** — 잘못된 설정은 잘못된 채로 드러나야 한다.
        value = {"mode": "all", "all": {"start": "22:00", "end": "02:00", "end_offset_days": 0}}
        assert resolve_day_range(value, MON) is None

    def test_zero_length_vs_full_day(self):
        zero = {"mode": "all", "all": {"start": "11:00", "end": "11:00", "end_offset_days": 0}}
        full = {"mode": "all", "all": {"start": "11:00", "end": "11:00", "end_offset_days": 1}}
        s, e = resolve_day_range(zero, MON)
        assert e - s == 0
        s, e = resolve_day_range(full, MON)
        assert e - s == MINUTES_PER_DAY

    @pytest.mark.parametrize("offset", [-1, True, "1", None])
    def test_bad_offset_treated_as_zero(self, offset):
        value = {"mode": "all", "all": {"start": "06:00", "end": "23:00", "end_offset_days": offset}}
        assert resolve_day_range(value, MON) == (360, 1380)


class TestResolveDayRangePerDay:
    def test_uses_weekday_entry(self):
        value = {
            "mode": "per_day",
            "all": {"start": "06:00", "end": "23:00", "end_offset_days": 0},
            "per_day": {"mon": {"start": "09:00", "end": "17:00", "end_offset_days": 0}},
        }
        assert resolve_day_range(value, MON) == (540, 1020)

    def test_missing_weekday_falls_back_to_all_not_closed(self):
        # 요일 키를 지우는 것은 휴무가 아니라 all 폴백이다.
        value = {
            "mode": "per_day",
            "all": {"start": "06:00", "end": "23:00", "end_offset_days": 0},
            "per_day": {"mon": {"start": "09:00", "end": "17:00", "end_offset_days": 0}},
        }
        assert resolve_day_range(value, TUE) == (360, 1380)


class TestClosedDays:
    def test_closed_weekday_has_no_range(self):
        value = {
            "mode": "all",
            "all": {"start": "11:00", "end": "22:00", "end_offset_days": 0},
            "closed": ["sun"],
        }
        assert is_closed_weekday(value, SUN) is True
        assert resolve_day_range(value, SUN) is None
        assert is_closed_weekday(value, MON) is False
        assert resolve_day_range(value, MON) == (660, 1320)

    def test_closed_wins_over_per_day_entry(self):
        value = {
            "mode": "per_day",
            "all": {"start": "11:00", "end": "22:00", "end_offset_days": 0},
            "per_day": {"sun": {"start": "12:00", "end": "18:00", "end_offset_days": 0}},
            "closed": ["sun"],
        }
        assert resolve_day_range(value, SUN) is None

    def test_missing_or_malformed_closed(self):
        assert is_closed_weekday({}, MON) is False
        assert is_closed_weekday({"closed": "mon"}, MON) is False
        assert is_closed_weekday(None, MON) is False


class TestResolveDayRangeGarbage:
    @pytest.mark.parametrize("value", [None, {}, {"all": None}, {"all": {"start": "26:00", "end": "02:00"}}, "06:00-23:00"])
    def test_returns_none(self, value):
        assert resolve_day_range(value, MON) is None


class TestStoreDayStartFromOrg:
    def test_wraps_in_all_key(self):
        assert store_day_start_from_org(time(6, 0)) == {"all": "06:00"}
        assert store_day_start_from_org(time(17, 30)) == {"all": "17:30"}

    def test_none_stays_none(self):
        # org 미설정 → 매장도 미설정으로 두고 런타임 기본값(06:00)에 맡긴다.
        assert store_day_start_from_org(None) is None


# ── 저장 시점 형태 검증 (F3) ─────────────────────────────────

class TestValidateDayRangeSetting:
    """파서가 깨진 값을 조용히 '미설정'으로 떨어뜨리므로 입구에서 막아야 한다."""

    KEY = "store.operating_hours"

    def test_unregistered_key_is_not_checked(self):
        from app.utils.timezone import validate_day_range_setting
        validate_day_range_setting("some.other.key", {"garbage": True})  # 예외 없음

    def test_valid_value_passes(self):
        from app.utils.timezone import validate_day_range_setting
        validate_day_range_setting(self.KEY, {
            "mode": "all",
            "all": {"start": "11:00", "end": "02:00", "end_offset_days": 1},
            "per_day": {}, "closed": ["mon"],
        })

    def test_unset_all_is_allowed(self):
        """미설정 = 제한 없음. 빈 객체를 거절하면 안 된다."""
        from app.utils.timezone import validate_day_range_setting
        validate_day_range_setting(self.KEY, {"mode": "all", "all": {}, "per_day": {}, "closed": []})

    def test_24_plus_notation_is_rejected(self):
        """폐기한 표기가 되살아나면 SV 공백 검사가 조용히 멈춘다."""
        from app.utils.timezone import validate_day_range_setting
        with pytest.raises(ValueError, match="24"):
            validate_day_range_setting(self.KEY, {
                "mode": "all", "all": {"start": "03:00", "end": "26:00"}, "per_day": {}, "closed": [],
            })

    def test_bad_offset_is_rejected(self):
        from app.utils.timezone import validate_day_range_setting
        with pytest.raises(ValueError, match="end_offset_days"):
            validate_day_range_setting(self.KEY, {
                "mode": "all", "all": {"start": "09:00", "end": "17:00", "end_offset_days": 2},
                "per_day": {}, "closed": [],
            })

    def test_per_day_entry_is_checked(self):
        from app.utils.timezone import validate_day_range_setting
        with pytest.raises(ValueError, match="per_day.mon"):
            validate_day_range_setting(self.KEY, {
                "mode": "per_day", "all": {}, "closed": [],
                "per_day": {"mon": {"start": "09:00", "end": "99:99"}},
            })

    def test_non_object_is_rejected(self):
        from app.utils.timezone import validate_day_range_setting
        with pytest.raises(ValueError):
            validate_day_range_setting(self.KEY, "11:00-22:00")
