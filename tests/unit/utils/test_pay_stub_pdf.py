"""Unit — pay stub PDF 빌더 (app/utils/pay_stub_pdf.py, E4).

fpdf2 는 TTF 서브셋 + 압축 스트림이라 본문 텍스트를 바이트에서 직접 검증할 수
없다. 대신:
    - %PDF- 매직바이트 + non-trivial 크기 (렌더 성공)
    - 내용이 늘면 (멀티 rate 구간, penalty 사유, 일자별 행) 산출물 크기도
      늘어남 (내용 반영 근거)
    - 페이지 트리의 /Count (압축 대상 아님) 로 자동 페이지 브레이크 검증
    - calc_version 불일치 → BadRequestError (동결 계약 가드)
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.schemas.payroll import (
    CALC_VERSION,
    ContextDay,
    DayDetail,
    EntryBreakdown,
    PenaltyLine,
    RateSegment,
    WorkedBreak,
    WorkedShift,
)
from app.utils.exceptions import BadRequestError
from app.utils.pay_stub_pdf import (
    build_pay_stub_pdf,
    context_days_note,
    day_amounts_line,
    day_label,
    day_premium_total,
    day_total,
    worked_times_line,
)

_START = date(2026, 7, 1)
_END = date(2026, 7, 15)


_RATE = Decimal("20.00")


def _day(
    work_date: date,
    *,
    regular: int = 480,
    ot: int = 0,
    with_amounts: bool = True,
) -> DayDetail:
    """일별 상세 1건 ($20/hr). with_amounts=False = 금액 필드 생기기 전 동결본."""
    if not with_amounts:
        return DayDetail(
            work_date=work_date, regular_minutes=regular, ot_minutes=ot,
            applied_rate=_RATE,
        )
    reg_amount = (Decimal(regular) / 60 * _RATE).quantize(Decimal("0.01"))
    ot_amount = (Decimal(ot) / 60 * _RATE * Decimal("1.5")).quantize(Decimal("0.01"))
    return DayDetail(
        work_date=work_date, regular_minutes=regular, ot_minutes=ot,
        applied_rate=_RATE,
        regular_amount=reg_amount,
        ot_amount=ot_amount,
        dt_amount=Decimal("0.00"),
        total_amount=reg_amount + ot_amount,
    )


def _breakdown(
    segments: list[RateSegment],
    penalties: list[PenaltyLine] | None = None,
    days: list[DayDetail] | None = None,
) -> dict:
    return EntryBreakdown(
        calc_version=CALC_VERSION,
        segments=segments,
        days=days if days is not None else [_day(date(2026, 7, 6))],
        penalties=penalties or [],
        tip_period_id=None,
    ).model_dump(mode="json")


def _entry(breakdown: dict, **overrides) -> SimpleNamespace:
    """PayrollEntry duck-type — 빌더가 읽는 필드만."""
    base = dict(
        id=uuid.uuid4(),
        pay_period_id=uuid.uuid4(),
        member_name="Stub Tester",
        empid=7,
        crewid=1234,
        revision=0,
        regular_minutes=480,
        ot_minutes=0,
        dt_minutes=0,
        regular_pay=Decimal("160.00"),
        ot_pay=Decimal("0.00"),
        dt_pay=Decimal("0.00"),
        penalty_pay=Decimal("0.00"),
        card_tips=Decimal("0.00"),
        gross_pay=Decimal("160.00"),
        calc_version=CALC_VERSION,
        breakdown=breakdown,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_PERIOD = SimpleNamespace(start_date=_START, end_date=_END)
_STORE = SimpleNamespace(name="Test Store", address="123 Main St, Los Angeles, CA")
_ORG = SimpleNamespace(name="Test Organization LLC")


def _single_rate_entry() -> SimpleNamespace:
    return _entry(
        _breakdown(
            [
                RateSegment(
                    rate=Decimal("20.00"),
                    regular_minutes=480,
                    amount=Decimal("160.00"),
                )
            ]
        )
    )


def test_single_rate_stub_is_parseable_pdf() -> None:
    pdf = build_pay_stub_pdf(_single_rate_entry(), _PERIOD, _STORE, _ORG)
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 2000  # 폰트 임베드 + 본문 — trivial 산출물 방지


def test_multi_rate_raise_week_renders_all_segments() -> None:
    """멀티 rate (기간 중 인상) — 구간 2개 + OT 가 추가로 렌더된다."""
    single = build_pay_stub_pdf(_single_rate_entry(), _PERIOD, _STORE, _ORG)

    multi_entry = _entry(
        _breakdown(
            [
                RateSegment(
                    rate=Decimal("20.00"),
                    regular_minutes=480,
                    amount=Decimal("160.00"),
                ),
                RateSegment(
                    rate=Decimal("25.00"),
                    regular_minutes=480,
                    ot_minutes=120,
                    amount=Decimal("275.00"),
                ),
            ]
        ),
        regular_minutes=960,
        ot_minutes=120,
        regular_pay=Decimal("360.00"),
        ot_pay=Decimal("75.00"),
        gross_pay=Decimal("435.00"),
    )
    multi = build_pay_stub_pdf(multi_entry, _PERIOD, _STORE, _ORG)
    assert multi.startswith(b"%PDF-")
    # 구간 행 + OT 주석이 추가 — 산출물이 단일 rate 보다 커야 한다
    assert len(multi) > len(single)


def test_penalty_lines_with_reasons_render() -> None:
    """penalty 라인 + reason 텍스트 (C5) — 사유가 렌더에 포함된다."""
    base = build_pay_stub_pdf(_single_rate_entry(), _PERIOD, _STORE, _ORG)

    penalty_entry = _entry(
        _breakdown(
            [
                RateSegment(
                    rate=Decimal("20.00"),
                    regular_minutes=480,
                    amount=Decimal("160.00"),
                )
            ],
            penalties=[
                PenaltyLine(
                    work_date=date(2026, 7, 6),
                    kind="meal_penalty",
                    reason="Meal break shorter than 30 minutes on a shift over 5 hours",
                    amount=Decimal("20.00"),
                ),
                PenaltyLine(
                    work_date=date(2026, 7, 6),
                    kind="rest_penalty",
                    reason="Rest break not provided for a 8.0 hour shift",
                    amount=Decimal("20.00"),
                ),
            ],
        ),
        penalty_pay=Decimal("40.00"),
        gross_pay=Decimal("200.00"),
    )
    with_penalties = build_pay_stub_pdf(penalty_entry, _PERIOD, _STORE, _ORG)
    assert with_penalties.startswith(b"%PDF-")
    # 사유 2건이 추가로 렌더 — 산출물 크기 증가로 반영 확인
    assert len(with_penalties) > len(base)


def test_missing_ids_and_address_render_placeholders() -> None:
    """empid/crewid/주소 미보유 — 빈칸 대신 상태 문구 (조용한 누락 방지)."""
    entry = _entry(
        _breakdown(
            [
                RateSegment(
                    rate=Decimal("20.00"),
                    regular_minutes=480,
                    amount=Decimal("160.00"),
                )
            ]
        ),
        empid=None,
        crewid=None,
    )
    store_no_addr = SimpleNamespace(name="Test Store", address=None)
    pdf = build_pay_stub_pdf(entry, _PERIOD, store_no_addr, _ORG)
    assert pdf.startswith(b"%PDF-")


def test_empty_breakdown_zero_hours_still_renders() -> None:
    """근무 0 인 entry 도 렌더 실패 없이 명세서 생성."""
    entry = _entry(
        _breakdown([]),
        regular_minutes=0,
        regular_pay=Decimal("0.00"),
        gross_pay=Decimal("0.00"),
    )
    entry.breakdown["days"] = []
    pdf = build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)
    assert pdf.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# Daily detail — 일자별 상세 표
# ---------------------------------------------------------------------------


def _seg() -> RateSegment:
    return RateSegment(
        rate=_RATE, regular_minutes=480, amount=Decimal("160.00")
    )


def test_day_label_shows_weekday() -> None:
    """상세 표 날짜 셀 — "Jul 20 (Mon)" (콘솔 Day detail 과 같은 표기)."""
    assert day_label(date(2026, 7, 20)) == "Jul 20 (Mon)"
    assert day_label(date(2026, 7, 5)) == "Jul 5 (Sun)"  # 1자리 날짜 0 패딩 없음
    assert day_label(date(2026, 12, 31)) == "Dec 31 (Thu)"


class TestWorkedTimesLine:
    """일자별 행 아래 보조 라인 — 근무/휴게 벽시계."""

    @staticmethod
    def _day(shifts: list[WorkedShift], breaks: list[WorkedBreak]) -> DayDetail:
        return DayDetail(work_date=date(2026, 7, 20), shifts=shifts, breaks=breaks)

    def test_full_line(self) -> None:
        line = worked_times_line(
            self._day(
                [WorkedShift(start="08:00", end="16:30")],
                [
                    WorkedBreak(start="10:00", end="10:10", type="paid_10min"),
                    WorkedBreak(start="12:00", end="12:30", type="unpaid_meal"),
                    WorkedBreak(start="14:30", end="14:40", type="paid_10min"),
                ],
            )
        )
        assert line == "Worked 08:00-16:30 · Meal 12:00-12:30 · Rest 10:00, 14:30"

    def test_split_shift_lists_both_windows(self) -> None:
        line = worked_times_line(
            self._day(
                [
                    WorkedShift(start="09:00", end="13:00"),
                    WorkedShift(start="17:00", end="21:00"),
                ],
                [],
            )
        )
        assert line == "Worked 09:00-13:00, 17:00-21:00"

    def test_overnight_shift_keeps_wall_clock(self) -> None:
        line = worked_times_line(
            self._day([WorkedShift(start="22:00", end="06:30")], [])
        )
        assert line == "Worked 22:00-06:30"

    def test_open_shift_without_end(self) -> None:
        line = worked_times_line(self._day([WorkedShift(start="08:00")], []))
        assert line == "Worked 08:00-"

    def test_no_records_gives_empty_line(self) -> None:
        """옛 동결본/전기 frozen 일자 — 보조 라인 자체를 생략한다."""
        assert worked_times_line(self._day([], [])) == ""


class TestDayAmountBreakdown:
    """일별 금액 내역 + Day total — premium 은 penalties[] 에서 파생 (스키마 무증설)."""

    _WORK = date(2026, 7, 20)

    @staticmethod
    def _penalty(work_date: date, amount: str, kind: str = "meal_penalty"):
        return PenaltyLine(
            work_date=work_date, kind=kind,
            reason="Worked 6.5h with no 30-min meal break", amount=Decimal(amount),
        )

    def _day(self, **overrides) -> DayDetail:
        base = dict(
            work_date=self._WORK, regular_minutes=390,
            applied_rate=Decimal("18.00"),
            regular_amount=Decimal("117.00"),
            ot_amount=Decimal("0.00"),
            dt_amount=Decimal("0.00"),
            total_amount=Decimal("117.00"),
        )
        base.update(overrides)
        return DayDetail(**base)

    def test_premium_total_sums_only_that_day(self) -> None:
        penalties = [
            self._penalty(self._WORK, "18.00"),
            self._penalty(self._WORK, "18.00", kind="rest_penalty"),
            self._penalty(date(2026, 7, 21), "18.00"),  # 다른 날 — 섞이면 안 된다
        ]
        assert day_premium_total(penalties, self._WORK) == Decimal("36.00")

    def test_premium_total_zero_without_penalties(self) -> None:
        assert day_premium_total([], self._WORK) == Decimal("0.00")
        assert day_premium_total(None, self._WORK) == Decimal("0.00")

    def test_day_total_adds_premium_to_work_pay(self) -> None:
        """사용자 지적 지점 — 근무 $117 + premium $36 = 그날 실지급 $153."""
        assert day_total(self._day(), Decimal("36.00")) == Decimal("153.00")

    def test_day_total_is_none_for_legacy_day(self) -> None:
        legacy = DayDetail(work_date=self._WORK, regular_minutes=390)
        assert day_total(legacy, Decimal("36.00")) is None

    def test_amounts_line_omits_zero_items(self) -> None:
        line = day_amounts_line(self._day(), Decimal("36.00"))
        assert line == "Regular $ 117.00 · Premium $ 36.00"  # OT/DT 0 → 생략

    def test_amounts_line_lists_every_nonzero_item(self) -> None:
        day = self._day(
            regular_minutes=480, ot_minutes=120, dt_minutes=60,
            regular_amount=Decimal("144.00"),
            ot_amount=Decimal("54.00"),
            dt_amount=Decimal("36.00"),
            total_amount=Decimal("234.00"),
        )
        assert day_amounts_line(day, Decimal("18.00")) == (
            "Regular $ 144.00 · OT $ 54.00 · DT $ 36.00 · Premium $ 18.00"
        )

    def test_amounts_line_empty_without_premium_or_pay(self) -> None:
        zero = self._day(
            regular_amount=Decimal("0.00"), total_amount=Decimal("0.00")
        )
        assert day_amounts_line(zero, Decimal("0.00")) == ""

    def test_amounts_line_empty_for_legacy_day(self) -> None:
        """금액을 모르는 옛 동결본 — premium 만 있는 반쪽 내역은 안 보여준다."""
        legacy = DayDetail(work_date=self._WORK, regular_minutes=390)
        assert day_amounts_line(legacy, Decimal("36.00")) == ""


class TestContextDaysNote:
    """경계 걸친 주 각주 — 왜 이 기간에 OT 가 걸렸는지."""

    def test_note_lists_span_and_hours(self) -> None:
        note = context_days_note(
            [
                ContextDay(work_date=date(2026, 7, 12), net_minutes=480),
                ContextDay(work_date=date(2026, 7, 15), net_minutes=480),
            ]
        )
        assert "2 day(s) worked in the prior period" in note
        assert "Jul 12 (Sun) - Jul 15 (Wed)" in note
        assert "16.00h" in note
        assert "prior period's statement" in note

    def test_single_day_has_no_range(self) -> None:
        note = context_days_note(
            [ContextDay(work_date=date(2026, 7, 15), net_minutes=300)]
        )
        assert "Jul 15 (Wed), 5.00h" in note
        assert " - " not in note.split("(")[1]  # 범위 표기 없음

    def test_empty_context_is_omitted(self) -> None:
        assert context_days_note([]) == ""


def _page_count(pdf: bytes) -> int:
    """페이지 트리의 /Count — 페이지 객체는 압축 대상이 아니라 바이트에서 읽힌다."""
    match = re.search(rb"/Count\s+(\d+)", pdf)
    assert match is not None, "page tree /Count not found"
    return int(match.group(1))


def test_daily_detail_lists_every_day() -> None:
    """일자별 행이 실제로 늘어난다 — 하루짜리보다 5일짜리 산출물이 크다."""
    one_day = build_pay_stub_pdf(_single_rate_entry(), _PERIOD, _STORE, _ORG)

    five_days = _entry(
        _breakdown(
            [_seg()],
            days=[_day(date(2026, 7, 6) + timedelta(days=i)) for i in range(5)],
        ),
        regular_minutes=2400,
        regular_pay=Decimal("800.00"),
        gross_pay=Decimal("800.00"),
    )
    pdf = build_pay_stub_pdf(five_days, _PERIOD, _STORE, _ORG)

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > len(one_day)


def test_daily_detail_annotates_the_day_that_has_a_penalty() -> None:
    """penalty 는 그날 행 아래 사유+금액으로 덧붙는다 (일자 대조 가능)."""
    days = [_day(date(2026, 7, 6)), _day(date(2026, 7, 7))]
    plain = build_pay_stub_pdf(
        _entry(_breakdown([_seg()], days=days)), _PERIOD, _STORE, _ORG
    )
    annotated = build_pay_stub_pdf(
        _entry(
            _breakdown(
                [_seg()],
                penalties=[
                    PenaltyLine(
                        work_date=date(2026, 7, 6),
                        kind="meal_penalty",
                        reason="Worked 10.0h with no 30-min meal break",
                        amount=Decimal("20.00"),
                    )
                ],
                days=days,
            ),
            penalty_pay=Decimal("20.00"),
            gross_pay=Decimal("180.00"),
        ),
        _PERIOD, _STORE, _ORG,
    )
    assert annotated.startswith(b"%PDF-")
    assert len(annotated) > len(plain)


def test_daily_detail_totals_row_and_amount_sublines_render() -> None:
    """합계 행 + 금액 내역 서브라인 경로 — penalty 낀 다일 시나리오가 렌더된다.

    (텍스트는 서브셋 폰트라 바이트 검증 불가 — 값 자체는 순수 헬퍼 테스트가,
    배치는 렌더 이미지 확인이 담당한다.)
    """
    days = [_day(date(2026, 7, 6)), _day(date(2026, 7, 7), ot=120)]
    penalties = [
        PenaltyLine(
            work_date=date(2026, 7, 6), kind="meal_penalty",
            reason="Worked 8.0h with no 30-min meal break", amount=Decimal("20.00"),
        )
    ]
    plain = build_pay_stub_pdf(
        _entry(_breakdown([_seg()], days=days)), _PERIOD, _STORE, _ORG
    )
    with_premium = build_pay_stub_pdf(
        _entry(
            _breakdown([_seg()], penalties=penalties, days=days),
            penalty_pay=Decimal("20.00"),
            gross_pay=Decimal("400.00"),
        ),
        _PERIOD, _STORE, _ORG,
    )
    assert with_premium.startswith(b"%PDF-")
    assert _page_count(with_premium) == 2
    # premium 서브라인 + 사유 라인이 더 붙는다
    assert len(with_premium) > len(plain)


def test_daily_detail_legacy_days_without_amounts_render() -> None:
    """금액 필드가 없던 옛 동결본 — 재계산 없이 '—' 로 렌더 (실패 없음)."""
    entry = _entry(
        _breakdown(
            [_seg()],
            days=[
                _day(date(2026, 7, 6), with_amounts=False),
                _day(date(2026, 7, 7), with_amounts=False),
            ],
        )
    )
    pdf = build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000


def test_half_month_daily_detail_flows_onto_next_page() -> None:
    """반월 16일 + 일별 penalty 주석 — 자동 페이지 브레이크로 2페이지 이상."""
    days = [_day(_START + timedelta(days=i), ot=120) for i in range(16)]
    penalties = [
        PenaltyLine(
            work_date=_START + timedelta(days=i),
            kind="meal_penalty",
            reason="Worked 10.0h with no 30-min meal break",
            amount=Decimal("20.00"),
        )
        for i in range(16)
    ]
    entry = _entry(
        _breakdown(
            [
                RateSegment(
                    rate=_RATE, regular_minutes=7680, ot_minutes=1920,
                    amount=Decimal("3520.00"),
                )
            ],
            penalties=penalties,
            days=days,
        ),
        regular_minutes=7680,
        ot_minutes=1920,
        regular_pay=Decimal("2560.00"),
        ot_pay=Decimal("960.00"),
        penalty_pay=Decimal("320.00"),
        gross_pay=Decimal("3840.00"),
    )
    pdf = build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)

    assert pdf.startswith(b"%PDF-")
    # 16일+penalty 상세는 2장(상세 시작)을 넘어 3장 이상으로 흐른다
    assert _page_count(pdf) >= 3
    # 상세는 항상 새 페이지에서 시작 — 단일일 stub 도 최소 2장 (1장 요약 + 2장 상세)
    assert _page_count(build_pay_stub_pdf(
        _single_rate_entry(), _PERIOD, _STORE, _ORG
    )) == 2


def test_empty_daily_detail_still_gets_its_own_page() -> None:
    """근무 0 인 명세서도 1장=요약 / 2장=상세 구조는 유지 (빈 표 대신 안내 문구)."""
    entry = _entry(
        _breakdown([], days=[]),
        regular_minutes=0,
        regular_pay=Decimal("0.00"),
        gross_pay=Decimal("0.00"),
    )
    pdf = build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)
    assert pdf.startswith(b"%PDF-")
    assert _page_count(pdf) == 2


def test_unsupported_calc_version_raises() -> None:
    """동결 포맷 버전 불일치 — 조용한 오독 대신 BadRequestError."""
    entry = _single_rate_entry()
    entry.breakdown["calc_version"] = 999
    with pytest.raises(BadRequestError) as exc:
        build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)
    assert "calc_version" in str(exc.value.detail)


def test_unreadable_breakdown_raises() -> None:
    entry = _single_rate_entry()
    entry.breakdown = {"segments": "not-a-list"}
    with pytest.raises(BadRequestError):
        build_pay_stub_pdf(entry, _PERIOD, _STORE, _ORG)
