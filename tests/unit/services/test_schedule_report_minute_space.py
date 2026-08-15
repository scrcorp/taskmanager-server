"""Unit tests — sv_gap 의 분(minute) 공간 (P0-2).

예전 코드는 `start_time`/`end_time` shim(= 날짜를 버린 time)을 읽고
"끝 <= 시작이면 +1440" 으로 자정 넘김을 되짚었다. 그런데 빼기의 기준이 되는 창은
`resolve_day_range` 가 주는데, 그쪽 종료분은 `end_offset_days` 가 반영돼 정당하게
1440 을 넘는다. 두 값이 서로 다른 공간에 놓이는 순간 갭 계산이 통째로 틀어져
**SV 가 배치된 날에 "매장 하루 전체 no SV"** 가 나왔다.

여기서 고정하는 성질: 분 계산의 기준은 언제나 `operating_day` 라벨이고,
날짜 차이는 암묵 보정이 아니라 실제 날짜에서 나온다.
(CLAUDE.md — Time Representation Policy 금지사항 4번)
"""

from datetime import date, datetime

from app.services.schedule_report_service import _minutes_from_operating_day

FRIDAY = date(2026, 8, 14)
SATURDAY = date(2026, 8, 15)


class TestSameDay:
    def test_morning(self):
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 14, 11, 0)) == 660

    def test_midnight_start_of_label_day(self):
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 14, 0, 0)) == 0

    def test_late_evening(self):
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 14, 23, 59)) == 1439


class TestCrossMidnight:
    """영업일 라벨보다 하루 뒤 날짜를 가진 시각 — 1440 이상이 되어야 한다."""

    def test_next_day_midnight_is_1440(self):
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 0, 0)) == 1440

    def test_next_day_2am(self):
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 2, 0)) == 1560

    def test_next_day_6am(self):
        # 예전 코드의 실패 지점: 06:00 을 360 으로 읽고 "360 > 0 이니 보정 불필요" 로
        # 판단해 창(660, 1560) 에서 아무것도 깎지 못했다.
        assert _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 6, 0)) == 1800


class TestRegression:
    def test_overnight_sv_shift_lands_inside_window(self):
        """11:00→02:00(+1) 창에 배치된 SV 시프트가 실제로 창 안에 들어온다."""
        from app.services.schedule_report_service import _subtract_intervals

        window = (660, 1560)  # 11:00 ~ 02:00(+1)
        s = _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 14, 18, 0))
        e = _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 2, 0))
        assert (s, e) == (1080, 1560)

        gaps = _subtract_intervals(window[0], window[1], [(s, e)])
        # 18:00~02:00 이 SV 로 덮였으므로 남는 갭은 11:00~18:00 하나뿐이어야 한다.
        assert gaps == [(660, 1080)]

    def test_sv_shift_starting_after_midnight_is_not_dropped(self):
        """라벨=금, 실제 근무 토 00:00~06:00 — 예전엔 (0,360) 이 되어 창 밖으로 밀렸다."""
        from app.services.schedule_report_service import _subtract_intervals

        s = _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 0, 0))
        e = _minutes_from_operating_day(FRIDAY, datetime(2026, 8, 15, 6, 0))
        assert (s, e) == (1440, 1800)

        gaps = _subtract_intervals(660, 1560, [(s, e)])
        # 창 종료(1560=02:00+1)까지는 덮이므로 갭은 11:00~24:00 만 남는다.
        # 핵심은 "창 전체가 갭" 이 아니라는 것 — 예전 버그는 [(660, 1560)] 을 냈다.
        assert gaps == [(660, 1440)]
        assert gaps != [(660, 1560)]


class TestNone:
    def test_none_returns_none(self):
        assert _minutes_from_operating_day(FRIDAY, None) is None
