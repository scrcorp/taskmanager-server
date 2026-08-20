"""Unit — payroll_export_service 순수 규칙 (Payroll v1 Phase 4).

대상: app/services/payroll_export_service.py
    - minutes_to_hours: 분 → 소수 시간 (2dp HALF_UP)
    - format_rates: 멀티 rate 콤마 목록 / 중복 제거 / 2dp 고정
    - entry_export_row / preview_export_row: DUMMY 포맷 v1 셀 매핑 (공용 규칙)
    - build_export_workbook / build_draft_workbook: 시트 구성 + DRAFT 배너
    - export_filename: 매장명 sanitize (ASCII 안전) + draft prefix

DB 없음 — PayrollEntry 모델 / PayrollPreviewRow 를 직접 조립해 분기 전부 커버.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import PayrollEntry
from app.schemas.payroll import (
    CALC_VERSION,
    DayDetail,
    EntryBreakdown,
    PayrollPreviewRow,
    RateSegment,
)
from app.services.payroll_export_service import (
    DRAFT_BANNER,
    EXPORT_COLUMNS,
    EXPORT_SHEET_TITLE,
    RATE_CHANGES_COLUMNS,
    RATE_CHANGES_SHEET_TITLE,
    RateChangeExportRow,
    WARNINGS_SHEET_TITLE,
    build_draft_workbook,
    build_export_workbook,
    entry_export_row,
    export_filename,
    format_rates,
    minutes_to_hours,
    preview_export_row,
)
from app.utils.exceptions import BadRequestError

_MON = date(2026, 7, 6)


def _breakdown(*segments: RateSegment) -> EntryBreakdown:
    return EntryBreakdown(
        segments=list(segments),
        days=[DayDetail(work_date=_MON, regular_minutes=480)],
    )


def _segment(rate: str, minutes: int = 480, amount: str = "160.00") -> RateSegment:
    return RateSegment(
        rate=Decimal(rate), regular_minutes=minutes, amount=Decimal(amount)
    )


def _entry(
    *,
    empid: int | None = 7,
    crewid: int | None = 900123,
    name: str = "Export Unit",
    breakdown: EntryBreakdown | None = None,
) -> PayrollEntry:
    """DB 없는 PayrollEntry — 동결 스냅샷과 같은 모양의 값."""
    bd = breakdown or _breakdown(_segment("20.00"))
    return PayrollEntry(
        empid=empid,
        crewid=crewid,
        member_name=name,
        revision=0,
        regular_minutes=720,
        ot_minutes=120,
        dt_minutes=0,
        regular_pay=Decimal("240.00"),
        ot_pay=Decimal("60.00"),
        dt_pay=Decimal("0.00"),
        penalty_pay=Decimal("60.00"),
        card_tips=Decimal("50.00"),
        gross_pay=Decimal("410.00"),
        calc_version=CALC_VERSION,
        breakdown=bd.model_dump(mode="json"),
    )


def _preview_row(
    *,
    empid: int | None = 7,
    crewid: int | None = 900123,
    name: str = "Export Unit",
    breakdown: EntryBreakdown | None = None,
) -> PayrollPreviewRow:
    """미확정 기간 draft 의 원천 — 동결 entry 와 같은 값을 들고 있는 preview 행."""
    return PayrollPreviewRow(
        user_id=uuid_mod.uuid4(),
        member_name=name,
        empid=empid,
        crewid=crewid,
        regular_minutes=720,
        ot_minutes=120,
        dt_minutes=0,
        regular_pay=Decimal("240.00"),
        ot_pay=Decimal("60.00"),
        dt_pay=Decimal("0.00"),
        penalty_pay=Decimal("60.00"),
        card_tips=Decimal("50.00"),
        gross_pay=Decimal("410.00"),
        breakdown=breakdown or _breakdown(_segment("20.00")),
    )


# ---------------------------------------------------------------------------
# minutes_to_hours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, Decimal("0.00")),
        (90, Decimal("1.50")),
        (100, Decimal("1.67")),  # 1.666… → HALF_UP
        (719, Decimal("11.98")),  # 11.983…
        (720, Decimal("12.00")),
    ],
)
def test_minutes_to_hours(minutes: int, expected: Decimal) -> None:
    assert minutes_to_hours(minutes) == expected


# ---------------------------------------------------------------------------
# format_rates
# ---------------------------------------------------------------------------


def test_format_rates_single() -> None:
    assert format_rates(_breakdown(_segment("20.00"))) == "20.00"


def test_format_rates_multi_comma_list_preserves_order() -> None:
    bd = _breakdown(_segment("18.00"), _segment("22.50"))
    assert format_rates(bd) == "18.00, 22.50"


def test_format_rates_dedupes_and_quantizes() -> None:
    # "20" 과 "20.00" 은 같은 rate — 2dp 로 접혀 1개만
    bd = _breakdown(_segment("20"), _segment("20.00"))
    assert format_rates(bd) == "20.00"


def test_format_rates_empty_segments_blank() -> None:
    assert format_rates(_breakdown()) == ""


# ---------------------------------------------------------------------------
# entry_export_row — DUMMY 포맷 v1 매핑
# ---------------------------------------------------------------------------


def test_entry_export_row_full_values() -> None:
    entry = _entry()
    row = entry_export_row(
        entry, _breakdown(_segment("18.00"), _segment("22.00"))
    )
    assert len(row) == len(EXPORT_COLUMNS)
    assert row == [
        7,
        900123,
        "Export Unit",
        Decimal("12.00"),
        Decimal("2.00"),
        Decimal("0.00"),
        "18.00, 22.00",
        Decimal("240.00"),
        Decimal("60.00"),
        Decimal("0.00"),
        Decimal("60.00"),
        Decimal("50.00"),
        Decimal("410.00"),
    ]


def test_entry_export_row_empid_falls_back_to_crewid() -> None:
    entry = _entry(empid=None, crewid=555)
    row = entry_export_row(entry, _breakdown(_segment("20.00")))
    assert row[0] == 555  # EMPID 셀 = crewid fallback
    assert row[1] == 555  # CREWID 셀은 그대로 (fallback 대조 가능)


def test_entry_export_row_both_missing_blank() -> None:
    entry = _entry(empid=None, crewid=None)
    row = entry_export_row(entry, _breakdown(_segment("20.00")))
    assert row[0] is None
    assert row[1] is None


# ---------------------------------------------------------------------------
# preview_export_row — 미확정 원천도 같은 매핑 한 벌
# ---------------------------------------------------------------------------


def test_preview_export_row_matches_frozen_row_shape() -> None:
    """같은 값을 가진 preview 행과 동결 entry 는 셀 값이 완전히 같아야 한다."""
    bd = _breakdown(_segment("18.00"), _segment("22.00"))
    frozen = entry_export_row(_entry(), bd)
    drafted = preview_export_row(_preview_row(breakdown=bd))
    assert drafted == frozen
    assert len(drafted) == len(EXPORT_COLUMNS)
    assert drafted[6] == "18.00, 22.00"
    assert drafted[12] == Decimal("410.00")


def test_preview_export_row_empid_falls_back_to_crewid() -> None:
    row = preview_export_row(_preview_row(empid=None, crewid=555))
    assert row[0] == 555
    assert row[1] == 555


def test_preview_export_row_both_missing_blank() -> None:
    row = preview_export_row(_preview_row(empid=None, crewid=None))
    assert row[0] is None
    assert row[1] is None


# ---------------------------------------------------------------------------
# build_export_workbook — 시트 구성
# ---------------------------------------------------------------------------


def test_workbook_all_matched_has_no_warnings_sheet() -> None:
    wb = build_export_workbook([_entry()])
    assert wb.sheetnames == [EXPORT_SHEET_TITLE]
    ws = wb[EXPORT_SHEET_TITLE]
    assert [c.value for c in ws[1]] == EXPORT_COLUMNS
    assert ws.max_row == 2  # 헤더 + entry 1행


def test_workbook_unmatched_entry_gets_warnings_sheet() -> None:
    wb = build_export_workbook(
        [_entry(), _entry(empid=None, crewid=None, name="No Ids")]
    )
    assert wb.sheetnames == [EXPORT_SHEET_TITLE, WARNINGS_SHEET_TITLE]
    warn = wb[WARNINGS_SHEET_TITLE]
    assert warn.max_row == 2  # 헤더 + 미매칭 1명
    assert warn.cell(row=2, column=1).value == "No Ids"
    assert "EMPID" in warn.cell(row=2, column=2).value


def test_workbook_rejects_unknown_calc_version() -> None:
    entry = _entry()
    entry.breakdown = {**entry.breakdown, "calc_version": 999}
    with pytest.raises(BadRequestError):
        build_export_workbook([entry])


# ---------------------------------------------------------------------------
# build_draft_workbook — 배너 1행 + 그 아래 동일 헤더
# ---------------------------------------------------------------------------


def test_draft_workbook_has_banner_above_header() -> None:
    wb = build_draft_workbook([_preview_row()])
    ws = wb[EXPORT_SHEET_TITLE]
    assert ws.cell(row=1, column=1).value == DRAFT_BANNER
    assert [c.value for c in ws[2]] == EXPORT_COLUMNS  # 헤더는 배너 아래
    assert ws.max_row == 3  # 배너 + 헤더 + 행 1개
    assert ws.cell(row=3, column=3).value == "Export Unit"


def test_draft_workbook_row_values_match_frozen_export() -> None:
    """배너를 걷어내면 동결본 시트와 같은 행 — 포맷이 갈라지지 않았는지 확인."""
    frozen = build_export_workbook([_entry()])[EXPORT_SHEET_TITLE]
    draft = build_draft_workbook([_preview_row()])[EXPORT_SHEET_TITLE]
    assert [c.value for c in draft[3]] == [c.value for c in frozen[2]]


def test_draft_workbook_unmatched_gets_warnings_sheet() -> None:
    wb = build_draft_workbook(
        [_preview_row(), _preview_row(empid=None, crewid=None, name="No Ids")]
    )
    assert wb.sheetnames == [EXPORT_SHEET_TITLE, WARNINGS_SHEET_TITLE]
    warn = wb[WARNINGS_SHEET_TITLE]
    assert warn.max_row == 2
    assert warn.cell(row=2, column=1).value == "No Ids"
    assert "EMPID" in warn.cell(row=2, column=2).value


def test_frozen_workbook_has_no_banner() -> None:
    ws = build_export_workbook([_entry()])[EXPORT_SHEET_TITLE]
    assert ws.cell(row=1, column=1).value == EXPORT_COLUMNS[0]


# ---------------------------------------------------------------------------
# Rate Changes 시트
# ---------------------------------------------------------------------------


def _rate_change(
    *,
    name: str = "Export Unit",
    old_rate: str | None = "16.00",
    memo: str | None = "Set from payroll",
) -> RateChangeExportRow:
    return RateChangeExportRow(
        name=name,
        empid=7,
        old_rate=Decimal(old_rate) if old_rate is not None else None,
        new_rate=Decimal("18.00"),
        effective_date=_MON,
        memo=memo,
        changed_by="Manager Kim",
        changed_at="2026-07-08 17:30",
    )


def test_workbook_without_rate_changes_has_no_sheet() -> None:
    wb = build_export_workbook([_entry()])
    assert RATE_CHANGES_SHEET_TITLE not in wb.sheetnames


def test_workbook_rate_changes_sheet_rows() -> None:
    wb = build_export_workbook([_entry()], [_rate_change()])
    assert wb.sheetnames == [EXPORT_SHEET_TITLE, RATE_CHANGES_SHEET_TITLE]
    ws = wb[RATE_CHANGES_SHEET_TITLE]
    assert [c.value for c in ws[1]] == RATE_CHANGES_COLUMNS
    assert [c.value for c in ws[2]] == [
        "Export Unit",
        7,
        Decimal("16.00"),
        Decimal("18.00"),
        _MON.isoformat(),
        "Set from payroll",
        "Manager Kim",
        "2026-07-08 17:30",
    ]


def test_rate_changes_first_record_and_blank_memo() -> None:
    """old_rate NULL(최초 기록)·memo 없음도 빈칸으로 그대로 나간다."""
    wb = build_draft_workbook(
        [_preview_row()], [_rate_change(old_rate=None, memo=None)]
    )
    ws = wb[RATE_CHANGES_SHEET_TITLE]
    assert ws.cell(row=2, column=3).value is None  # Old Rate
    assert ws.cell(row=2, column=6).value is None  # Memo


def test_draft_workbook_keeps_banner_with_rate_changes() -> None:
    """draft 배너와 Rate Changes 시트가 공존 — 본 시트 모양은 불변."""
    wb = build_draft_workbook([_preview_row()], [_rate_change()])
    ws = wb[EXPORT_SHEET_TITLE]
    assert ws.cell(row=1, column=1).value == DRAFT_BANNER
    assert wb.sheetnames == [EXPORT_SHEET_TITLE, RATE_CHANGES_SHEET_TITLE]


# ---------------------------------------------------------------------------
# export_filename
# ---------------------------------------------------------------------------


def test_export_filename_scheme() -> None:
    name = export_filename("Main St", date(2026, 7, 1), date(2026, 7, 15))
    assert name == "Payroll_MainSt_2026-07-01~2026-07-15.xlsx"


def test_export_filename_draft_suffix() -> None:
    """미확정본은 _DRAFT 접미 — 저장된 파일만 봐도 구분된다."""
    name = export_filename(
        "Main St", date(2026, 7, 1), date(2026, 7, 15), draft=True
    )
    assert name == "Payroll_MainSt_2026-07-01~2026-07-15_DRAFT.xlsx"


def test_export_filename_keeps_unicode_store_name() -> None:
    """한글 매장명 보존 — 받는 사람이 어느 매장인지 알아야 한다."""
    name = export_filename("서울 2호점", date(2026, 7, 1), date(2026, 7, 15))
    assert name == "Payroll_서울2호점_2026-07-01~2026-07-15.xlsx"


def test_export_filename_replaces_only_illegal_characters() -> None:
    """파일시스템 불가 문자만 '_' — '.' '#' 처럼 합법 문자는 그대로 둔다."""
    name = export_filename("Main St. #2/A*B", date(2026, 7, 1), date(2026, 7, 15))
    assert name == "Payroll_MainSt.#2_A_B_2026-07-01~2026-07-15.xlsx"


def test_export_filename_blank_store_falls_back() -> None:
    assert (
        export_filename("///", date(2026, 7, 1), date(2026, 7, 15))
        == "Payroll_download_2026-07-01~2026-07-15.xlsx"
    )
