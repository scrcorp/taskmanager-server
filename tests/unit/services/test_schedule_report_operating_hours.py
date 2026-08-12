"""Unit tests — 일일 리포트의 영업시간 필터 (D2-3).

출처가 `stores.operating_hours` 컬럼에서 `store.operating_hours` 설정 키로 바뀌었다.
컬럼 시절엔 전 매장 NULL 이라 필터가 사실상 아무 일도 하지 않았으므로,
**미설정이 검사 범위를 좁히지 않는다**는 성질을 여기서 고정한다.
"""

from datetime import date, time

from app.services.schedule_report_service import _shift_within_operating_hours

# 2026-08-10 = Monday, 2026-08-16 = Sunday
MONDAY = date(2026, 8, 10)
SUNDAY = date(2026, 8, 16)

OPEN_11_22 = {
    "mode": "all",
    "all": {"start": "11:00", "end": "22:00", "end_offset_days": 0},
    "per_day": {},
    "closed": ["sun"],
}


class TestUnsetIsInclusive:
    def test_none_checks_everything(self):
        assert _shift_within_operating_hours(None, MONDAY, time(2, 0), time(5, 0)) is True

    def test_malformed_checks_everything(self):
        assert _shift_within_operating_hours({"nonsense": 1}, MONDAY, time(2, 0), time(5, 0)) is True

    def test_registry_default_checks_everything(self):
        # registry 기본값은 미설정(빈 칸). 야간 시프트도 검사 대상으로 남아야 한다 —
        # "온종일 열림"(00:00→+1d) 을 기본으로 뒀을 때 이게 깨졌다: 포함 관계 판정이라
        # 22:00–06:00 은 24시간 창에도 들어가지 못하고 리포트에서 빠졌다.
        unset = {"mode": "all", "all": {}, "per_day": {}, "closed": []}
        assert _shift_within_operating_hours(unset, MONDAY, time(22, 0), time(6, 0)) is True
        assert _shift_within_operating_hours(unset, SUNDAY, time(12, 0), time(20, 0)) is True


class TestWithinWindow:
    def test_inside(self):
        assert _shift_within_operating_hours(OPEN_11_22, MONDAY, time(12, 0), time(20, 0)) is True

    def test_starts_before_open(self):
        assert _shift_within_operating_hours(OPEN_11_22, MONDAY, time(9, 0), time(20, 0)) is False

    def test_ends_after_close(self):
        assert _shift_within_operating_hours(OPEN_11_22, MONDAY, time(12, 0), time(23, 0)) is False

    def test_missing_shift_times_are_checked(self):
        # 시프트 시간을 모르면 비교할 수 없다 → 보수적으로 검사 대상.
        assert _shift_within_operating_hours(OPEN_11_22, MONDAY, None, None) is True


class TestClosedDay:
    def test_closed_day_skips_all_shifts(self):
        # 휴무는 "검사 없음", 미설정은 "전부 검사" — 정반대라 구분돼야 한다.
        assert _shift_within_operating_hours(OPEN_11_22, SUNDAY, time(12, 0), time(20, 0)) is False


class TestOvernightShift:
    def test_overnight_shift_against_overnight_hours(self):
        night = {"mode": "all", "all": {"start": "18:00", "end": "03:00", "end_offset_days": 1}}
        assert _shift_within_operating_hours(night, MONDAY, time(20, 0), time(2, 0)) is True
        assert _shift_within_operating_hours(night, MONDAY, time(20, 0), time(4, 0)) is False
