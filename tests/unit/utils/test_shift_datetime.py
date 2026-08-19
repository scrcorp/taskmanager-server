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


class TestOperatingDayWindow:
    """영업일 창 `[day_start(D), day_start(D+1))` — 시작 달력일 파생 규칙의 짝.

    이 창과 파생 규칙(`so = 시작시각 < day_start(D+1) ? 1 : 0`)이 갈리면 저장은
    통과하는데 근태가 그 시프트를 못 찾는 상태가 만들어진다(2026-08 오염 사고).
    """

    TZ = "America/Los_Angeles"
    CFG = {"all": "11:00"}

    def test_window_spans_boundary_to_boundary(self):
        from zoneinfo import ZoneInfo

        from app.utils.timezone import operating_day_window

        start, end = operating_day_window(self.TZ, self.CFG, date(2026, 8, 18))
        zi = ZoneInfo(self.TZ)
        assert start == datetime(2026, 8, 18, 11, 0, tzinfo=zi)
        assert end == datetime(2026, 8, 19, 11, 0, tzinfo=zi)

    def test_weekday_specific_boundaries_are_used_on_each_end(self):
        from app.utils.timezone import operating_day_window

        # 2026-08-18 은 화요일, 다음 날은 수요일.
        cfg = {"all": "11:00", "wed": "07:00"}
        start, end = operating_day_window(self.TZ, cfg, date(2026, 8, 18))
        assert start.strftime("%H:%M") == "11:00"
        assert end.strftime("%H:%M") == "07:00", "끝은 D+1 요일의 경계다"

    def test_operating_day_of_is_the_inverse(self):
        from app.utils.timezone import operating_day_of

        # 경계 11:00 — 09:00 은 전날 영업일, 17:00 은 그날 영업일.
        assert operating_day_of(self.CFG, datetime(2026, 8, 19, 9, 0)) == date(2026, 8, 18)
        assert operating_day_of(self.CFG, datetime(2026, 8, 19, 17, 0)) == date(2026, 8, 19)
        # 경계 정각은 그날에 속한다(창의 시작은 포함).
        assert operating_day_of(self.CFG, datetime(2026, 8, 19, 11, 0)) == date(2026, 8, 19)

    def test_window_and_derivation_agree_on_the_dawn_shift(self):
        from zoneinfo import ZoneInfo

        from app.utils.timezone import operating_day_window

        # 영업일 8/18 의 새벽조 09:00 시작 → 달력상 8/19 09:00 (so=1). 창 안이어야 한다.
        start, end = operating_day_window(self.TZ, self.CFG, date(2026, 8, 18))
        dawn = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(self.TZ))
        assert start <= dawn < end
        # 반면 8/19 17:00(= 사고 데이터)은 8/18 의 창 밖이다.
        corrupted = datetime(2026, 8, 19, 17, 0, tzinfo=ZoneInfo(self.TZ))
        assert not (start <= corrupted < end)


class TestDayStartMap:
    def test_partial_config_is_expanded_to_every_weekday(self):
        from app.utils.timezone import day_start_map

        got = day_start_map({"all": "11:00", "sat": "09:00"})
        assert set(got) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        assert got["sat"] == "09:00"
        assert got["mon"] == "11:00"

    def test_missing_config_falls_back_to_the_server_default(self):
        from app.utils.timezone import DEFAULT_DAY_START_TIME, day_start_map

        assert set(day_start_map(None).values()) == {DEFAULT_DAY_START_TIME}
