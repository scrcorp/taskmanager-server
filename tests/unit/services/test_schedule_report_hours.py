"""Unit tests — 발송 시각 파싱 (org 설정 schedule.report_times).

크론은 매시 정각에 깨어나고, 지금이 그 org 의 발송 시각인지 여기서 판단한다.
파싱이 조용히 틀리면 그 회차가 소리 없이 사라지므로 경계를 못 박아 둔다.
"""

import pytest

from app.services.schedule_report_service import parse_report_hours


class TestNormalCases:
    def test_default_three_times(self):
        assert parse_report_hours("7,15,22") == [7, 15, 22]

    def test_whitespace_is_tolerated(self):
        assert parse_report_hours(" 7 , 15 ,22 ") == [7, 15, 22]

    def test_single_hour(self):
        assert parse_report_hours("15") == [15]

    def test_result_is_sorted(self):
        assert parse_report_hours("22,7,15") == [7, 15, 22]

    def test_duplicates_collapse(self):
        assert parse_report_hours("7,7,15") == [7, 15]


class TestOffMeansOff:
    """빈 값은 "안 보내기" 다 — 조용히 기본값으로 되돌리면 끄는 방법이 없어진다."""

    @pytest.mark.parametrize("raw", ["", "   ", ",", ", ,", None])
    def test_empty_disables_sending(self, raw):
        assert parse_report_hours(raw) == []


class TestBoundaries:
    def test_midnight_and_last_hour_are_valid(self):
        assert parse_report_hours("0,23") == [0, 23]

    @pytest.mark.parametrize("raw", ["24", "-1", "99"])
    def test_out_of_range_is_dropped(self, raw):
        assert parse_report_hours(raw) == []

    def test_garbage_is_dropped_but_good_values_survive(self):
        """한 항목이 깨졌다고 나머지 회차까지 잃으면 안 된다."""
        assert parse_report_hours("7,abc,15") == [7, 15]

    def test_float_like_is_dropped(self):
        assert parse_report_hours("7.5,15") == [15]
