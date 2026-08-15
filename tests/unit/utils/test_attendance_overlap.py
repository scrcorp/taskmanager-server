"""Unit tests — app/utils/attendance_overlap (겹친 근무 구간 판정, 페이즈 ⑤).

이 모듈은 **이중 지급을 막는 최후 방어선**의 판정부다. 급여 확정 게이트
(`overlapping_attendance`)와 auto clock-out 제외가 모두 여기 결과를 그대로 쓴다.
그래서 여기서 고정하는 것은 "돈이 새는가" 를 가르는 경계들이다:

  - 맞닿은 구간(한쪽 종료 == 다른 쪽 시작)은 겹침이 **아니다**
  - 초는 버린다 — 화면상 09:00/09:00 인 두 shift 가 초 차이로 겹침이 되면
    사람이 볼 수 없는 이유로 급여 확정이 막힌다
  - 아직 안 닫힌 근무는 `now` 까지 열려 있다 (지금 두 shift 에 동시 출근 = 겹침)
  - 겹치는 **쌍의 양쪽 모두** 표시된다 (매니저가 어느 쪽을 먼저 열지 알 수 없다)
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.utils.attendance_overlap import (
    OverlapInterval,
    build_interval,
    intervals_overlap,
    overlapping_keys,
    overlapping_keys_from_rows,
)


NOW = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)


def _t(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 13, hour, minute, second, tzinfo=timezone.utc)


def _iv(key: str, start: datetime, end: datetime) -> OverlapInterval:
    return OverlapInterval(key=key, start=start, end=end)


# ---------------------------------------------------------------------------
# build_interval
# ---------------------------------------------------------------------------


def test_no_clock_in_means_no_interval() -> None:
    """출근 기록이 없으면 겹칠 구간 자체가 없다 — 예정만으로는 급여가 나가지 않는다."""
    assert build_interval("a", None, _t(13), now=NOW) is None


def test_open_shift_occupies_the_current_minute() -> None:
    """미퇴근 근무는 **지금 이 분의 끝**까지 열려 있는 것으로 본다.

    `now` 에서 끊으면 방금 찍은 근무의 구간이 길이 0 이 되어, "지금 두 shift 에
    동시에 출근해 있다" 는 이 판정이 존재하는 바로 그 상황이 안 잡힌다.
    """
    interval = build_interval("a", _t(9), None, now=_t(18, 0, 42))
    assert interval is not None
    assert interval.start == _t(9)
    assert interval.end == _t(18, 1)


def test_two_shifts_clocked_in_within_the_same_minute_overlap() -> None:
    """★ 겹침 clock-in 직후의 모습 — 라벨이 그 자리에서 붙어야 한다.

    앞 근무를 13:05:10 에, 뒤 근무를 13:05:50 에 찍으면 분 절삭 후 두 시작이 같다.
    구간을 `now` 에서 끊던 시절엔 둘 다 길이 0 이라 겹침이 아니었고, 그래서 anomaly
    가 clock-in 시점에 **한 번도** 붙지 않았다.
    """
    now = _t(13, 5, 50)
    rows = [("first", _t(13, 5, 10), None), ("second", _t(13, 5, 50), None)]
    assert overlapping_keys_from_rows(rows, now=now) == {"first", "second"}


def test_open_shift_starting_exactly_now_overlaps_the_earlier_open_one() -> None:
    """앞 근무가 13:00 부터 열려 있고 지금 13:05 에 또 찍은 경우."""
    now = _t(13, 5)
    rows = [("first", _t(13, 0), None), ("second", now, None)]
    assert overlapping_keys_from_rows(rows, now=now) == {"first", "second"}


def test_closed_shift_ending_when_an_open_one_starts_is_not_an_overlap() -> None:
    """열린 구간을 늘려도 오탐이 생기지 않는다 — 교대 인수인계의 정상 모습."""
    now = _t(18)
    rows = [("closed", _t(9), _t(13)), ("open", _t(13), None)]
    assert overlapping_keys_from_rows(rows, now=now) == set()


def test_seconds_are_floored_on_both_ends() -> None:
    """판정은 분 경계로. 초를 살리면 화면과 기록이 갈린다."""
    interval = build_interval("a", _t(9, 0, 59), _t(13, 0, 59), now=NOW)
    assert interval is not None
    assert interval.start == _t(9)
    assert interval.end == _t(13)


def test_reversed_record_becomes_zero_length_not_dropped() -> None:
    """정정 실수로 뒤집힌 기록(퇴근 < 출근)은 버리지 않되 남을 덮지 않는다.

    버리면 그 row 는 어떤 겹침 판정에도 참여하지 못해 조용히 사라진다.
    """
    interval = build_interval("a", _t(13), _t(9), now=NOW)
    assert interval is not None
    assert interval.start == interval.end == _t(13)


# ---------------------------------------------------------------------------
# intervals_overlap — 경계
# ---------------------------------------------------------------------------


def test_touching_intervals_do_not_overlap() -> None:
    """한쪽 종료 == 다른 쪽 시작은 겹침이 아니다 (연속 근무의 정상 모습)."""
    a = _iv("a", _t(9), _t(13))
    b = _iv("b", _t(13), _t(17))
    assert intervals_overlap(a, b) is False


def test_one_minute_intersection_is_an_overlap() -> None:
    a = _iv("a", _t(9), _t(13, 1))
    b = _iv("b", _t(13), _t(17))
    assert intervals_overlap(a, b) is True


def test_zero_length_interval_never_overlaps() -> None:
    a = _iv("a", _t(13), _t(13))
    b = _iv("b", _t(9), _t(17))
    assert intervals_overlap(a, b) is False


def test_containment_is_an_overlap() -> None:
    outer = _iv("outer", _t(9), _t(17))
    inner = _iv("inner", _t(11), _t(12))
    assert intervals_overlap(outer, inner) is True
    assert intervals_overlap(inner, outer) is True


# ---------------------------------------------------------------------------
# overlapping_keys
# ---------------------------------------------------------------------------


def test_both_sides_of_an_overlapping_pair_are_flagged() -> None:
    """한쪽만 표시하면 "어느 화면에서 봐도 드러난다" 는 목적을 잃는다."""
    a = _iv("a", _t(9), _t(14))
    b = _iv("b", _t(13), _t(17))
    assert overlapping_keys([a, b]) == {"a", "b"}


def test_non_overlapping_day_flags_nothing() -> None:
    a = _iv("a", _t(9), _t(13))
    b = _iv("b", _t(14), _t(17))
    assert overlapping_keys([a, b]) == set()


def test_only_the_overlapping_pair_is_flagged_not_the_innocent_third() -> None:
    """정상 근무까지 싸잡아 표시하면 매니저가 무엇을 고쳐야 할지 알 수 없다."""
    a = _iv("a", _t(9), _t(14))
    b = _iv("b", _t(13), _t(17))
    clean = _iv("clean", _t(19), _t(22))
    assert overlapping_keys([a, b, clean]) == {"a", "b"}


def test_three_way_overlap_flags_everyone_involved() -> None:
    a = _iv("a", _t(9), _t(17))
    b = _iv("b", _t(10), _t(11))
    c = _iv("c", _t(12), _t(13))
    assert overlapping_keys([a, b, c]) == {"a", "b", "c"}


def test_input_order_does_not_change_the_result() -> None:
    """정렬 전제가 깨지면 조용히 겹침을 놓친다 — 순서 무관을 못 박는다."""
    a = _iv("a", _t(13), _t(17))
    b = _iv("b", _t(9), _t(14))
    assert overlapping_keys([a, b]) == overlapping_keys([b, a]) == {"a", "b"}


# ---------------------------------------------------------------------------
# overlapping_keys_from_rows — 호출부가 실제로 쓰는 입구
# ---------------------------------------------------------------------------


def test_from_rows_skips_rows_without_clock_in() -> None:
    rows = [
        ("scheduled_only", None, None),
        ("worked", _t(9), _t(13)),
    ]
    assert overlapping_keys_from_rows(rows, now=NOW) == set()


def test_from_rows_flags_two_open_shifts_as_overlapping() -> None:
    """겹침 clock-in(D15) 직후의 모습 — 둘 다 열려 있으니 `now` 까지 겹친다."""
    rows = [
        ("morning", _t(9), None),
        ("evening", _t(13, 5), None),
    ]
    assert overlapping_keys_from_rows(rows, now=NOW) == {"morning", "evening"}


def test_from_rows_flags_two_closed_shifts_that_overlap() -> None:
    """★ 이중 지급은 **퇴근 후**에 일어난다 — 둘 다 닫혔어도 겹치면 잡아야 한다.

    "열린 건만" 으로 좁히면 게이트가 실제로 돈이 새는 케이스를 통과시킨다.
    """
    rows = [
        ("a", _t(9), _t(14)),
        ("b", _t(13), _t(17)),
    ]
    assert overlapping_keys_from_rows(rows, now=NOW) == {"a", "b"}


def test_from_rows_back_to_back_shifts_are_clean() -> None:
    """연속 근무(13:00 종료 → 13:00 시작)는 정상이다 — 여기서 오탐이 나면
    멀쩡한 급여 확정이 매번 막힌다."""
    rows = [
        ("a", _t(9), _t(13)),
        ("b", _t(13), _t(17)),
    ]
    assert overlapping_keys_from_rows(rows, now=NOW) == set()
