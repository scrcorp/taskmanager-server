"""fixed_schedule.expand 단위 테스트 — 순수 함수, DB 없음.

계약 §7: 요일 매핑(0=Sun)·유효기간·assignable_until(None/날짜/키없음=차단)·
overnight end_at+1d·빈 byday. 추가로 break·여러 패턴 혼합·date_from>date_to.

달력 기준: 2026-08-02 는 일요일. 2026-08-02(Sun) ~ 2026-08-08(Sat) 이 한 주.
"""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.fixed_schedule import Occurrence, dow_sun0, expand

SUN = date(2026, 8, 2)
assert SUN.weekday() == 6  # 파이썬 weekday 로 일요일 확인

USER = uuid4()
STORE = uuid4()


def _pattern(**kw):
    base = dict(
        id=uuid4(),
        group_id=uuid4(),
        user_id=USER,
        store_id=STORE,
        work_role_id=None,
        byday=[1, 2, 3, 4, 5],
        start_time=time(9, 0),
        end_time=time(17, 0),
        break_start_time=None,
        break_end_time=None,
        start_date=date(2026, 1, 1),
        until_date=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _expand(patterns, date_from, date_to, assignable=None):
    if assignable is None:
        assignable = {USER: None}
    return expand(patterns, date_from=date_from, date_to=date_to, assignable_until=assignable)


def _dates(occs):
    return [o.occurrence_date for o in occs]


class TestDowMapping:
    def test_dow_sun0_sunday_is_zero_and_saturday_is_six(self):
        assert dow_sun0(SUN) == 0
        assert dow_sun0(SUN + timedelta(days=1)) == 1  # Mon
        assert dow_sun0(SUN + timedelta(days=6)) == 6  # Sat

    def test_byday_zero_matches_only_sundays(self):
        p = _pattern(byday=[0])
        occs = _expand([p], SUN, SUN + timedelta(days=13))
        assert _dates(occs) == [SUN, SUN + timedelta(days=7)]

    def test_byday_six_matches_only_saturdays(self):
        p = _pattern(byday=[6])
        occs = _expand([p], SUN, SUN + timedelta(days=6))
        assert _dates(occs) == [date(2026, 8, 8)]

    def test_weekday_pattern_skips_weekend(self):
        p = _pattern(byday=[1, 2, 3, 4, 5])
        occs = _expand([p], SUN, SUN + timedelta(days=6))
        assert _dates(occs) == [date(2026, 8, d) for d in (3, 4, 5, 6, 7)]
        assert all(o.occurrence_date.weekday() < 5 for o in occs)

    def test_python_weekday_is_not_used_directly(self):
        # 파이썬 weekday 로 잘못 매핑하면 byday=[0] 이 월요일을 잡는다 — 그러면 안 됨
        p = _pattern(byday=[0])
        occs = _expand([p], date(2026, 8, 3), date(2026, 8, 3))  # Monday only
        assert occs == []


class TestValidityPeriod:
    def test_start_date_inclusive(self):
        p = _pattern(byday=list(range(7)), start_date=date(2026, 8, 4))
        occs = _expand([p], SUN, date(2026, 8, 8))
        assert _dates(occs)[0] == date(2026, 8, 4)
        assert date(2026, 8, 3) not in _dates(occs)

    def test_until_date_inclusive(self):
        p = _pattern(byday=list(range(7)), until_date=date(2026, 8, 5))
        occs = _expand([p], SUN, date(2026, 8, 8))
        assert _dates(occs) == [SUN, date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]

    def test_until_none_is_unbounded(self):
        p = _pattern(byday=list(range(7)), until_date=None)
        far = date(2030, 12, 25)
        occs = _expand([p], far, far)
        assert _dates(occs) == [far]

    def test_window_entirely_before_start_date(self):
        p = _pattern(byday=list(range(7)), start_date=date(2027, 1, 1))
        assert _expand([p], SUN, date(2026, 8, 8)) == []

    def test_window_entirely_after_until_date(self):
        p = _pattern(byday=list(range(7)), until_date=date(2026, 7, 1))
        assert _expand([p], SUN, date(2026, 8, 8)) == []

    def test_window_edges_inclusive(self):
        p = _pattern(byday=list(range(7)))
        occs = _expand([p], date(2026, 8, 3), date(2026, 8, 5))
        assert _dates(occs) == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]

    def test_date_from_after_date_to_is_empty(self):
        p = _pattern(byday=list(range(7)))
        assert _expand([p], date(2026, 8, 5), date(2026, 8, 3)) == []


class TestAssignableUntil:
    def test_none_means_no_limit(self):
        p = _pattern(byday=list(range(7)))
        occs = _expand([p], SUN, date(2026, 8, 8), {USER: None})
        assert len(occs) == 7

    def test_date_cuts_off_after_inclusive(self):
        p = _pattern(byday=list(range(7)))
        occs = _expand([p], SUN, date(2026, 8, 8), {USER: date(2026, 8, 4)})
        assert _dates(occs) == [SUN, date(2026, 8, 3), date(2026, 8, 4)]

    def test_missing_key_is_fail_closed(self):
        p = _pattern(byday=list(range(7)))
        assert _expand([p], SUN, date(2026, 8, 8), {}) == []
        # 다른 사람 키만 있어도 차단
        assert _expand([p], SUN, date(2026, 8, 8), {uuid4(): None}) == []

    def test_cutoff_before_window_is_empty(self):
        p = _pattern(byday=list(range(7)))
        assert _expand([p], SUN, date(2026, 8, 8), {USER: date(2026, 7, 31)}) == []

    def test_gate_applied_per_user(self):
        other = uuid4()
        p1 = _pattern(byday=list(range(7)))
        p2 = _pattern(byday=list(range(7)), user_id=other)
        occs = _expand([p1, p2], SUN, date(2026, 8, 3), {USER: None, other: SUN})
        assert sorted((o.user_id, o.occurrence_date) for o in occs) == sorted(
            [(USER, SUN), (USER, date(2026, 8, 3)), (other, SUN)]
        )


class TestWallClockAssembly:
    def test_same_day_shift(self):
        p = _pattern(byday=[1], start_time=time(9, 0), end_time=time(17, 30))
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert o.occurrence_date == date(2026, 8, 3)
        assert o.start_at == datetime(2026, 8, 3, 9, 0)
        assert o.end_at == datetime(2026, 8, 3, 17, 30)
        assert o.break_start_at is None and o.break_end_at is None

    def test_overnight_end_at_plus_one_day(self):
        p = _pattern(byday=[1], start_time=time(22, 0), end_time=time(2, 0))
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert o.occurrence_date == date(2026, 8, 3)  # operating_day 는 시작일
        assert o.start_at == datetime(2026, 8, 3, 22, 0)
        assert o.end_at == datetime(2026, 8, 4, 2, 0)

    def test_overnight_does_not_create_occurrence_on_next_day_dow(self):
        # 월요일 22시~화요일 2시: byday=[1](Mon) 만이면 화요일엔 새 occurrence 없음
        p = _pattern(byday=[1], start_time=time(22, 0), end_time=time(2, 0))
        occs = _expand([p], date(2026, 8, 3), date(2026, 8, 4))
        assert _dates(occs) == [date(2026, 8, 3)]

    def test_break_same_day(self):
        p = _pattern(
            byday=[1], start_time=time(9, 0), end_time=time(17, 0),
            break_start_time=time(12, 0), break_end_time=time(12, 30),
        )
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert o.break_start_at == datetime(2026, 8, 3, 12, 0)
        assert o.break_end_at == datetime(2026, 8, 3, 12, 30)

    def test_break_after_midnight_in_overnight_shift_rolls_to_next_day(self):
        p = _pattern(
            byday=[1], start_time=time(22, 0), end_time=time(6, 0),
            break_start_time=time(1, 0), break_end_time=time(1, 30),
        )
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert o.break_start_at == datetime(2026, 8, 4, 1, 0)
        assert o.break_end_at == datetime(2026, 8, 4, 1, 30)

    def test_break_before_midnight_in_overnight_shift_stays_same_day(self):
        p = _pattern(
            byday=[1], start_time=time(22, 0), end_time=time(6, 0),
            break_start_time=time(23, 0), break_end_time=time(23, 30),
        )
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert o.break_start_at == datetime(2026, 8, 3, 23, 0)
        assert o.break_end_at == datetime(2026, 8, 3, 23, 30)

    def test_occurrence_carries_identity_fields(self):
        role = uuid4()
        p = _pattern(byday=[1], work_role_id=role)
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert isinstance(o, Occurrence)
        assert (o.pattern_id, o.group_id, o.user_id, o.store_id, o.work_role_id) == (
            p.id, p.group_id, USER, STORE, role,
        )

    def test_occurrence_is_frozen(self):
        p = _pattern(byday=[1])
        (o,) = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        with pytest.raises(AttributeError):
            o.start_at = datetime(2026, 1, 1)  # type: ignore[misc]


class TestEdgeAndMix:
    def test_empty_byday_yields_nothing(self):
        p = _pattern(byday=[])
        assert _expand([p], SUN, date(2026, 8, 8)) == []

    def test_empty_patterns(self):
        assert _expand([], SUN, date(2026, 8, 8)) == []

    def test_duplicate_byday_values_do_not_duplicate_occurrences(self):
        p = _pattern(byday=[1, 1, 1])
        occs = _expand([p], date(2026, 8, 3), date(2026, 8, 3))
        assert len(occs) == 1

    def test_multiple_patterns_mixed_and_sorted(self):
        other = uuid4()
        morning = _pattern(byday=[1, 3], start_time=time(9, 0), end_time=time(13, 0))
        evening = _pattern(byday=[1], start_time=time(17, 0), end_time=time(22, 0))
        other_p = _pattern(byday=[2], user_id=other, until_date=date(2026, 8, 4))
        occs = _expand(
            [evening, other_p, morning], SUN, date(2026, 8, 8), {USER: None, other: None},
        )
        got = [(o.occurrence_date, o.start_at.time(), o.pattern_id) for o in occs]
        assert got == [
            (date(2026, 8, 3), time(9, 0), morning.id),
            (date(2026, 8, 3), time(17, 0), evening.id),
            (date(2026, 8, 4), time(9, 0), other_p.id),
            (date(2026, 8, 5), time(9, 0), morning.id),
        ]
        # 두 블록이 같은 group 일 수도, 다를 수도 있음 — expand 는 group 을 그대로 전달만
        assert {o.group_id for o in occs if o.pattern_id == morning.id} == {morning.group_id}

    def test_same_group_two_blocks_share_group_id(self):
        gid = uuid4()
        a = _pattern(byday=[1], group_id=gid, start_time=time(9, 0), end_time=time(12, 0))
        b = _pattern(byday=[1], group_id=gid, start_time=time(13, 0), end_time=time(17, 0))
        occs = _expand([a, b], date(2026, 8, 3), date(2026, 8, 3))
        assert len(occs) == 2 and {o.group_id for o in occs} == {gid}

    def test_long_window_count(self):
        # 52주 × 5일 = 260 (2026-08-02 Sun 부터 364일: 정확히 52주)
        p = _pattern(byday=[1, 2, 3, 4, 5])
        occs = _expand([p], SUN, SUN + timedelta(days=363))
        assert len(occs) == 260
