"""interpret_clock_time 단위 테스트 — AK-1 수동 시각 입력 UTC 정규화.

수동 clock 시각 입력(naive/offset 명시)이 항상 매장 타임존 기준의 올바른
UTC instant 로 정규화되는지 검증. naive 값을 디바이스/서버 로컬 타임존으로
해석하던 payroll 버그 회귀 방지.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.utils.timezone import interpret_clock_time


class TestInterpretClockTime:
    def test_naive_interpreted_as_store_tz_winter(self) -> None:
        """naive 09:00 + LA(PST, UTC-8) → 17:00 UTC."""
        result = interpret_clock_time(
            datetime(2026, 1, 15, 9, 0), "America/Los_Angeles"
        )
        assert result == datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)
        assert result.utcoffset() is not None
        assert result.utcoffset().total_seconds() == 0

    def test_naive_interpreted_as_store_tz_dst(self) -> None:
        """naive 09:00 + LA(PDT, UTC-7) → 16:00 UTC — DST 오프셋 반영."""
        result = interpret_clock_time(
            datetime(2026, 7, 15, 9, 0), "America/Los_Angeles"
        )
        assert result == datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)

    def test_naive_with_utc_store(self) -> None:
        """UTC 매장 (same-tz path) — naive 09:00 → 09:00 UTC 그대로."""
        result = interpret_clock_time(datetime(2026, 1, 15, 9, 0), "UTC")
        assert result == datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)

    def test_aware_utc_passthrough(self) -> None:
        """이미 UTC aware 인 값은 instant 불변."""
        src = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)
        assert interpret_clock_time(src, "America/Los_Angeles") == src

    def test_explicit_offset_respected_over_store_tz(self) -> None:
        """offset 명시 값은 그 offset 존중 — 매장 tz 로 재해석하지 않음."""
        src = datetime(2026, 1, 15, 18, 0, tzinfo=ZoneInfo("Asia/Seoul"))  # UTC+9
        result = interpret_clock_time(src, "America/Los_Angeles")
        assert result == datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)

    def test_result_differs_from_naive_as_utc_misread(self) -> None:
        """(회귀 문서화) naive 를 UTC 로 오독하면 instant 가 어긋난다."""
        naive = datetime(2026, 1, 15, 9, 0)
        correct = interpret_clock_time(naive, "America/Los_Angeles")
        misread_as_utc = naive.replace(tzinfo=timezone.utc)
        assert correct != misread_as_utc
