"""Unit tests for app.utils.break_end_policy — Stage J.

분기 전수 커버:
  - paid_10min × {0, 5, 10, 15} × reason 유무
  - unpaid_meal × {0, 15, 30, 32, 35, 40} × reason 유무
  - legacy types (paid_short, unpaid_long)
  - unknown type
"""

import pytest

from app.utils.break_end_policy import validate_break_end


class TestPaidShort:
    def test_under_10_minutes_rejects(self):
        msg = validate_break_end("paid_10min", 5, None)
        assert msg is not None
        assert "5m more" in msg
        assert "10-minute minimum" in msg

    def test_exactly_10_minutes_passes(self):
        assert validate_break_end("paid_10min", 10, None) is None

    def test_over_10_minutes_passes(self):
        assert validate_break_end("paid_10min", 25, None) is None

    def test_zero_minutes_rejects(self):
        msg = validate_break_end("paid_10min", 0, None)
        assert msg is not None
        assert "10m more" in msg

    def test_reason_does_not_matter_for_paid(self):
        # paid 는 시간 충족이면 reason 무관
        assert validate_break_end("paid_10min", 10, None) is None
        assert validate_break_end("paid_10min", 10, "anything") is None


class TestUnpaidMeal:
    def test_under_30_minutes_rejects(self):
        msg = validate_break_end("unpaid_meal", 18, None)
        assert msg is not None
        assert "12m more" in msg
        assert "30-minute minimum" in msg

    def test_zero_minutes_rejects(self):
        msg = validate_break_end("unpaid_meal", 0, None)
        assert msg is not None
        assert "30m more" in msg

    def test_exactly_30_minutes_passes_no_reason(self):
        assert validate_break_end("unpaid_meal", 30, None) is None

    def test_within_allowance_32_passes_no_reason(self):
        assert validate_break_end("unpaid_meal", 32, None) is None

    def test_34_minutes_passes_no_reason(self):
        # 35 미만은 reason 불필요
        assert validate_break_end("unpaid_meal", 34, None) is None

    def test_35_minutes_without_reason_rejects(self):
        msg = validate_break_end("unpaid_meal", 35, None)
        assert msg is not None
        assert "reason" in msg.lower()

    def test_40_minutes_without_reason_rejects(self):
        assert validate_break_end("unpaid_meal", 40, None) is not None

    def test_35_minutes_with_reason_passes(self):
        assert validate_break_end("unpaid_meal", 35, "Lunch ran long") is None

    def test_35_minutes_with_whitespace_only_reason_rejects(self):
        msg = validate_break_end("unpaid_meal", 35, "   ")
        assert msg is not None
        assert "reason" in msg.lower()

    def test_35_minutes_with_empty_reason_rejects(self):
        assert validate_break_end("unpaid_meal", 35, "") is not None


class TestLegacyTypes:
    def test_paid_short_normalized_to_paid_10min(self):
        # legacy paid_short → normalize → paid_10min, 10분 미만 reject
        msg = validate_break_end("paid_short", 5, None)
        assert msg is not None
        assert "10-minute minimum" in msg

    def test_paid_short_above_10_passes(self):
        assert validate_break_end("paid_short", 10, None) is None

    def test_unpaid_long_normalized_to_unpaid_meal(self):
        msg = validate_break_end("unpaid_long", 18, None)
        assert msg is not None
        assert "30-minute minimum" in msg

    def test_unpaid_long_above_35_requires_reason(self):
        assert validate_break_end("unpaid_long", 40, None) is not None
        assert validate_break_end("unpaid_long", 40, "reason") is None


class TestUnknownType:
    def test_unknown_type_passes(self):
        # 정의되지 않은 break_type 은 검증 skip (None 반환)
        assert validate_break_end("custom_break", 0, None) is None
        assert validate_break_end("foo", 1000, None) is None


@pytest.mark.parametrize(
    "break_type,minutes,reason,should_reject",
    [
        # paid
        ("paid_10min", 0, None, True),
        ("paid_10min", 9, None, True),
        ("paid_10min", 10, None, False),
        ("paid_10min", 11, None, False),
        # unpaid
        ("unpaid_meal", 0, None, True),
        ("unpaid_meal", 29, None, True),
        ("unpaid_meal", 30, None, False),
        ("unpaid_meal", 34, None, False),
        ("unpaid_meal", 35, None, True),
        ("unpaid_meal", 35, "ok", False),
        ("unpaid_meal", 60, None, True),
        ("unpaid_meal", 60, "ok", False),
    ],
)
def test_matrix(break_type, minutes, reason, should_reject):
    msg = validate_break_end(break_type, minutes, reason)
    if should_reject:
        assert msg is not None
    else:
        assert msg is None
