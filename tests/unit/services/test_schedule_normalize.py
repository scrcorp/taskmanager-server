"""ScheduleService._normalize_shift_input 단위 테스트.

전환기 구/신 입력 정규화 — 두 인코딩 동기화, operating_day 기본값, 자정 넘김, 신 필드 우선.
"""
from datetime import date, datetime, time

from app.services.schedule_service import ScheduleService

svc = ScheduleService()


def _norm(**kw):
    base = dict(
        work_date=None, operating_day=None,
        start_time=None, end_time=None, break_start_time=None, break_end_time=None,
        start_at=None, end_at=None, break_start_at=None, break_end_at=None,
    )
    base.update(kw)
    return svc._normalize_shift_input(**base)


class TestLegacyInput:
    def test_legacy_same_day(self):
        r = _norm(work_date=date(2026, 7, 8), start_time="09:00", end_time="17:00")
        assert r["operating_day"] == date(2026, 7, 8)
        assert r["start_at"] == datetime(2026, 7, 8, 9, 0)
        assert r["end_at"] == datetime(2026, 7, 8, 17, 0)

    def test_legacy_overnight_rolls_end(self):
        r = _norm(work_date=date(2026, 7, 8), start_time="22:00", end_time="02:00")
        assert r["start_at"] == datetime(2026, 7, 8, 22, 0)
        assert r["end_at"] == datetime(2026, 7, 9, 2, 0)

    def test_legacy_with_break(self):
        r = _norm(work_date=date(2026, 7, 8), start_time="09:00", end_time="17:00",
                  break_start_time="12:00", break_end_time="12:30")
        assert r["break_start_at"] == datetime(2026, 7, 8, 12, 0)
        assert r["break_end_at"] == datetime(2026, 7, 8, 12, 30)


class TestNewInput:
    def test_new_same_day(self):
        r = _norm(operating_day=date(2026, 7, 8),
                  start_at="2026-07-08T09:00", end_at="2026-07-08T17:00")
        assert r["operating_day"] == date(2026, 7, 8)
        assert r["start_at"] == datetime(2026, 7, 8, 9, 0)
        assert r["end_at"] == datetime(2026, 7, 8, 17, 0)
        # 구 컬럼 동기화

    def test_new_early_morning_explicit_date(self):
        # 영업일 7/8 이지만 실제 근무는 7/9 새벽 1시 (사용자 핵심 시나리오)
        r = _norm(operating_day=date(2026, 7, 8),
                  start_at="2026-07-09T01:00", end_at="2026-07-09T09:00")
        assert r["operating_day"] == date(2026, 7, 8)      # 영업일 라벨 유지
        assert r["start_at"] == datetime(2026, 7, 9, 1, 0)  # 실제 시각은 7/9

    def test_operating_day_defaults_to_start_at_date(self):
        r = _norm(start_at="2026-07-08T09:00", end_at="2026-07-08T17:00")
        assert r["operating_day"] == date(2026, 7, 8)

    def test_new_takes_precedence_over_legacy(self):
        # 신 필드가 있으면 구 필드 무시
        r = _norm(work_date=date(2020, 1, 1), start_time="05:00", end_time="06:00",
                  operating_day=date(2026, 7, 8),
                  start_at="2026-07-08T09:00", end_at="2026-07-08T17:00")
        assert r["start_at"] == datetime(2026, 7, 8, 9, 0)
        assert r["operating_day"] == date(2026, 7, 8)


class TestBreakGuards:
    """브레이크 짝/순서/포함 가드 — 역전·창밖 브레이크가 net을 오염시키던 구멍(적대 검증)."""

    def _norm_break(self, **kw):
        import pytest
        from app.utils.exceptions import BadRequestError
        with pytest.raises(BadRequestError):
            _norm(**kw)

    def test_inverted_break_rejected(self):
        # 14:00→13:00 (둘 다 start 이후, 같은 날) → 음수 브레이크 → 과지급이던 케이스
        self._norm_break(operating_day=date(2026, 7, 8),
                         start_at="2026-07-08T09:00", end_at="2026-07-08T17:00",
                         break_start_at="2026-07-08T14:00", break_end_at="2026-07-08T13:00")

    def test_break_outside_shift_rejected(self):
        self._norm_break(operating_day=date(2026, 7, 8),
                         start_at="2026-07-08T09:00", end_at="2026-07-08T17:00",
                         break_start_at="2026-07-08T09:00", break_end_at="2026-07-09T09:00")

    def test_half_break_pair_rejected(self):
        self._norm_break(operating_day=date(2026, 7, 8),
                         start_at="2026-07-08T09:00", end_at="2026-07-08T17:00",
                         break_start_at="2026-07-08T12:00")

    def test_legacy_wrap_break_outside_rejected(self):
        # 구 인코딩: break_end 08:00 < start 09:00 → +1d 앵커 → 창 밖 22h 브레이크이던 케이스
        self._norm_break(work_date=date(2026, 7, 8), start_time="09:00", end_time="17:00",
                         break_start_time="10:00", break_end_time="08:00")

    def test_overnight_shift_break_across_midnight_ok(self):
        # 정당한 케이스: 22:00~02:00(+1d) 근무의 자정 넘는 브레이크 00:30~01:00
        r = _norm(work_date=date(2026, 7, 8), start_time="22:00", end_time="02:00",
                  break_start_time="00:30", break_end_time="01:00")
        assert r["break_start_at"] == datetime(2026, 7, 9, 0, 30)
        assert r["break_end_at"] == datetime(2026, 7, 9, 1, 0)


class TestEmpty:
    def test_no_input(self):
        r = _norm()
        assert r["start_at"] is None
        assert r["end_at"] is None
        assert r["operating_day"] is None
