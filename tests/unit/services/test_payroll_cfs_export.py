"""CFS 급여 파일 양식의 순수 규칙 테스트 (DB 없음).

컬럼 구성·파생값·이름 표기·순번 부여를 검증한다. 근거는
docs/99_inbox/2026-08-11-payroll-cfs-export-결정사항.md.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.payroll_cfs_export_service import (
    CFS_COLUMNS,
    CfsRow,
    CfsSheet,
    build_workbook,
    cfs_name,
    daily_rounded_hours,
    merge_by_person,
    minutes_to_hours,
    payroll_id_for,
)


class _FakeUser:
    def __init__(self, first=None, middle=None, last=None, full=None, username=None):
        self.first_name = first
        self.middle_name = middle
        self.last_name = last
        self.full_name = full
        self.username = username


def _row(**kwargs) -> CfsRow:
    base = dict(
        corp="M KOREAN BBQ",
        payroll_id="2026.07.LH",
        name="TEST NAME (TEST)",
        emp_id=1001,
        rate=Decimal("16.90"),
        performance_bonus=Decimal("0.00"),
        rgl=Decimal("0.00"),
        ovr=Decimal("0.00"),
        dbl=Decimal("0.00"),
        tip_apply=0,
        earnedtip=Decimal("0.00"),
        performanceb=Decimal("0.00"),
    )
    base.update(kwargs)
    return CfsRow(**base)


# ── 컬럼 구성 ────────────────────────────────────────────────────────


def test_column_count_is_nineteen() -> None:
    """원본 21개 중 S열(4070)·T열(중복 earnedtip)을 뺀 19개."""
    assert len(CFS_COLUMNS) == 19


def test_dropped_columns_are_absent() -> None:
    assert "total 4070 earnedtip" not in CFS_COLUMNS
    assert CFS_COLUMNS.count("earnedtip") == 1
    assert not any(c.startswith("df2_") for c in CFS_COLUMNS)
    assert "cfs_name" not in CFS_COLUMNS
    assert "JobToDo" not in CFS_COLUMNS


def test_column_order_matches_source_file() -> None:
    assert CFS_COLUMNS[:5] == ["corp", "payroll_id", "no", "name", "emp_id"]
    assert CFS_COLUMNS[-1] == "premium pay"


# ── payroll_id ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "start,expected",
    [
        (date(2026, 7, 1), "2026.07.FH"),
        (date(2026, 7, 15), "2026.07.FH"),
        (date(2026, 7, 16), "2026.07.LH"),
        (date(2026, 12, 16), "2026.12.LH"),
        (date(2026, 1, 1), "2026.01.FH"),
    ],
)
def test_payroll_id_halves(start: date, expected: str) -> None:
    assert payroll_id_for(start) == expected


# ── 이름 표기 ────────────────────────────────────────────────────────


def test_name_uses_split_parts_uppercased() -> None:
    label, structured = cfs_name(
        _FakeUser(first="Brian", last="Baik", username="brian2")
    )
    assert label == "BRIAN BAIK (BRIAN2)"
    assert structured is True


def test_name_includes_middle_when_present() -> None:
    label, _ = cfs_name(
        _FakeUser(first="Lindsey", middle="E", last="Del Rosario", username="lindsey")
    )
    assert label == "LINDSEY E DEL ROSARIO (LINDSEY)"


def test_name_falls_back_to_full_name_and_flags_it() -> None:
    """구조화 이름이 없으면 폴백하되 '분리 안 됨'을 알려야 한다."""
    label, structured = cfs_name(_FakeUser(full="Miguel Canil", username="miguel"))
    assert label == "MIGUEL CANIL (MIGUEL)"
    assert structured is False


def test_name_without_username_has_no_parens() -> None:
    label, _ = cfs_name(_FakeUser(first="Rosa", last="Cano"))
    assert label == "ROSA CANO"


# ── 파생값 ───────────────────────────────────────────────────────────


def test_total_comp_is_rate_plus_bonus() -> None:
    row = _row(rate=Decimal("16.90"), performance_bonus=Decimal("1.60"))
    assert row.total_comp == Decimal("18.50")


def test_total_is_sum_of_three_buckets() -> None:
    row = _row(rgl=Decimal("84.70"), ovr=Decimal("5.41"), dbl=Decimal("0.00"))
    assert row.total == Decimal("90.11")


@pytest.mark.parametrize(
    "minutes,expected",
    [(0, "0.00"), (60, "1.00"), (90, "1.50"), (5407, "90.12"), (1, "0.02")],
)
def test_minutes_to_hours(minutes: int, expected: str) -> None:
    assert minutes_to_hours(minutes) == Decimal(expected)


# ── 시트 조립 ────────────────────────────────────────────────────────


def test_no_column_is_assigned_by_empid_order() -> None:
    """no 는 저장값이 아니라 시트에서 부여한다 — empid 오름차순."""
    rows = [
        _row(emp_id=1200, name="B", sort_key=(False, 1200, "B")),
        _row(emp_id=1010, name="A", sort_key=(False, 1010, "A")),
    ]
    wb = build_workbook([CfsSheet(title="odg", rows=rows)])
    ws = wb["odg"]
    assert [ws.cell(row=r, column=3).value for r in (2, 3)] == [1, 2]
    assert [ws.cell(row=r, column=5).value for r in (2, 3)] == [1010, 1200]


def test_rows_without_empid_sort_last() -> None:
    rows = [
        _row(emp_id=None, name="NO ID", sort_key=(True, 0, "NO ID")),
        _row(emp_id=5001, name="HAS ID", sort_key=(False, 5001, "HAS ID")),
    ]
    wb = build_workbook([CfsSheet(title="tbc", rows=rows)])
    ws = wb["tbc"]
    assert ws.cell(row=2, column=5).value == 5001
    assert ws.cell(row=3, column=5).value is None


def test_one_sheet_per_group() -> None:
    wb = build_workbook(
        [
            CfsSheet(title="odg", rows=[_row()]),
            CfsSheet(title="tbc", rows=[_row()]),
        ]
    )
    assert wb.sheetnames == ["odg", "tbc"]


def test_warnings_sheet_only_when_there_are_warnings() -> None:
    clean = build_workbook([CfsSheet(title="odg", rows=[_row()])])
    assert "Warnings" not in clean.sheetnames

    flagged = build_workbook(
        [CfsSheet(title="odg", rows=[_row(warnings=["No EMPID on file"])])]
    )
    assert "Warnings" in flagged.sheetnames
    assert flagged["Warnings"].cell(row=2, column=1).value == "odg"


def test_draft_sheet_pushes_header_down_one_row() -> None:
    wb = build_workbook([CfsSheet(title="odg", rows=[_row()], is_draft=True)])
    ws = wb["odg"]
    assert ws.cell(row=1, column=1).value.startswith("DRAFT")
    assert ws.cell(row=2, column=1).value == "corp"


def test_cells_match_column_order() -> None:
    row = _row(
        rate=Decimal("16.90"),
        performance_bonus=Decimal("1.75"),
        rgl=Decimal("84.70"),
        ovr=Decimal("5.41"),
        tip_apply=1,
        earnedtip=Decimal("42.37"),
        performanceb=Decimal("157.69"),
        note="new emp",
        cash=Decimal("3.00"),
        premium_pay=Decimal("16.90"),
    )
    cells = row.cells(no=7)
    assert len(cells) == len(CFS_COLUMNS)
    mapped = dict(zip(CFS_COLUMNS, cells))
    assert mapped["no"] == 7
    assert mapped["total_comp"] == Decimal("18.65")
    assert mapped["total"] == Decimal("90.11")
    assert mapped["tip_apply"] == 1
    assert mapped["performanceb"] == Decimal("157.69")
    assert mapped["note"] == "new emp"
    assert mapped["premium pay"] == Decimal("16.90")


# ── 그룹 내 인물 병합 ────────────────────────────────────────────────


def _person_row(user_id: str, **kwargs) -> CfsRow:
    row = _row(**kwargs)
    row.user_id = user_id
    return row


def test_same_person_across_two_stores_becomes_one_row() -> None:
    """회계사 파일은 그룹당 1인 1행 — 매장별로 두 줄이 나가면 안 된다."""
    rows = merge_by_person(
        [
            _person_row("u1", emp_id=5016, rgl=Decimal("60.00"), name="JESSE (JESSE)"),
            _person_row("u1", emp_id=5030, rgl=Decimal("6.97"), name="JESSE (JESSE)"),
        ]
    )
    assert len(rows) == 1
    assert rows[0].rgl == Decimal("66.97")


def test_merge_keeps_the_smaller_empid() -> None:
    """매장별로 따로 채번된 경우 처음 받은(작은) 번호를 남긴다."""
    rows = merge_by_person(
        [
            _person_row("u1", emp_id=5030),
            _person_row("u1", emp_id=5016),
        ]
    )
    assert rows[0].emp_id == 5016


def test_merge_sums_money_and_hours() -> None:
    rows = merge_by_person(
        [
            _person_row(
                "u1", emp_id=1, rgl=Decimal("10.00"), ovr=Decimal("2.00"),
                earnedtip=Decimal("5.00"), performanceb=Decimal("3.00"),
                cash=Decimal("1.00"), premium_pay=Decimal("16.90"),
            ),
            _person_row(
                "u1", emp_id=1, rgl=Decimal("5.00"), ovr=Decimal("1.00"),
                earnedtip=Decimal("2.50"), performanceb=Decimal("1.50"),
                cash=Decimal("0.50"), premium_pay=Decimal("16.90"),
            ),
        ]
    )
    merged = rows[0]
    assert merged.rgl == Decimal("15.00")
    assert merged.ovr == Decimal("3.00")
    assert merged.total == Decimal("18.00")
    assert merged.earnedtip == Decimal("7.50")
    assert merged.performanceb == Decimal("4.50")
    assert merged.cash == Decimal("1.50")
    assert merged.premium_pay == Decimal("33.80")


def test_merge_keeps_tip_eligible_if_any_store_says_so() -> None:
    """한 매장에서만 팁 대상이어도 그룹 행은 대상으로 표기한다."""
    rows = merge_by_person(
        [_person_row("u1", emp_id=1, tip_apply=0), _person_row("u1", emp_id=1, tip_apply=1)]
    )
    assert rows[0].tip_apply == 1


def test_merge_unions_notes_without_duplicates() -> None:
    rows = merge_by_person(
        [
            _person_row("u1", emp_id=1, note="new emp"),
            _person_row("u1", emp_id=1, note="new emp, rate change"),
        ]
    )
    assert rows[0].note == "new emp, rate change"


def test_different_people_are_not_merged() -> None:
    rows = merge_by_person(
        [_person_row("u1", emp_id=1), _person_row("u2", emp_id=2)]
    )
    assert len(rows) == 2


# ── 시간 산출: 일자별 반올림 합 ──────────────────────────────────────


class _FakeDay:
    def __init__(self, regular=0, ot=0, dt=0):
        self.regular_minutes = regular
        self.ot_minutes = ot
        self.dt_minutes = dt


def test_daily_rounded_hours_sums_per_day_rounding() -> None:
    """검산 가능한 값 — 일자별로 반올림한 뒤 더한다."""
    days = [_FakeDay(regular=364), _FakeDay(regular=61)]
    # 6.07 + 1.02 = 7.09  (분 합계 425분을 한 번에 반올림하면 7.08)
    assert daily_rounded_hours(days, "regular_minutes") == Decimal("7.09")


def test_daily_rounded_hours_differs_from_single_rounding() -> None:
    """이 방식이 '총분 한 번 반올림'과 다르다는 것을 못박아 둔다."""
    days = [_FakeDay(regular=364), _FakeDay(regular=61)]
    once = minutes_to_hours(sum(d.regular_minutes for d in days))
    assert once == Decimal("7.08")
    assert daily_rounded_hours(days, "regular_minutes") != once


def test_daily_rounded_hours_handles_empty_and_zero() -> None:
    assert daily_rounded_hours([], "regular_minutes") == Decimal("0.00")
    assert daily_rounded_hours([_FakeDay()], "ot_minutes") == Decimal("0.00")


def test_daily_rounded_hours_reads_the_requested_bucket() -> None:
    days = [_FakeDay(regular=480, ot=35), _FakeDay(regular=120, ot=10)]
    assert daily_rounded_hours(days, "regular_minutes") == Decimal("10.00")
    assert daily_rounded_hours(days, "ot_minutes") == Decimal("0.75")
    assert daily_rounded_hours(days, "dt_minutes") == Decimal("0.00")


def test_row_total_equals_sum_of_its_own_columns() -> None:
    """파일을 받은 쪽이 rgl+ovr+dbl 을 더하면 total 과 정확히 맞아야 한다."""
    row = _row(rgl=Decimal("96.90"), ovr=Decimal("6.12"), dbl=Decimal("0.00"))
    assert row.total == row.rgl + row.ovr + row.dbl == Decimal("103.02")
