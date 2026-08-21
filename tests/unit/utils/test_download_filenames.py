"""Unit — 다운로드 파일명/헤더 규칙 (app/utils/download.py).

한글 매장명·직원명이 파일명에서 사라지면 받는 사람이 구분을 못 한다. 그래서
이름은 유니코드 그대로 두고, HTTP 헤더에서만 규격(RFC 6266/5987)을 맞춘다:
    - filename*=UTF-8''… 에 원본
    - filename="…" 에는 ASCII fallback (헤더는 latin-1 — 비 ASCII 면 500)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.utils.download import (
    ascii_fallback,
    content_disposition,
    date_range_tag,
    export_filename,
    safe_filename,
)


# ---------------------------------------------------------------------------
# safe_filename — 불가 문자만 접고 유니코드 보존
# ---------------------------------------------------------------------------


def test_keeps_unicode() -> None:
    assert safe_filename("서울 2호점") == "서울2호점"
    assert safe_filename("김하늘") == "김하늘"


def test_replaces_filesystem_illegal_characters() -> None:
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_keeps_legal_punctuation() -> None:
    """'.' '#' '&' 는 파일명에 써도 되는 문자 — 굳이 바꾸지 않는다."""
    assert safe_filename("Main St. #2 & Co") == "MainSt.#2&Co"


def test_strips_control_characters() -> None:
    assert safe_filename("bad\x00name\x1f") == "bad_name"


def test_collapses_whitespace() -> None:
    assert safe_filename("  Main   St  ") == "MainSt"


def test_blank_name_falls_back() -> None:
    assert safe_filename("") == "download"
    assert safe_filename("///") == "download"
    assert safe_filename("   ") == "download"


# ---------------------------------------------------------------------------
# ascii_fallback / content_disposition
# ---------------------------------------------------------------------------


def test_ascii_fallback_drops_non_ascii_but_keeps_the_rest() -> None:
    """한글만 빠지고 ASCII 부분(숫자 포함)은 남는다."""
    assert ascii_fallback("Payroll_서울2호점_2026-07-01~2026-07-15.xlsx") == (
        "Payroll_2_2026-07-01~2026-07-15.xlsx"
    )


def test_ascii_fallback_keeps_pure_ascii_name() -> None:
    name = "PayStub_UmaHan_2026-07-16~2026-07-31_DRAFT.pdf"
    assert ascii_fallback(name) == name


def test_ascii_fallback_keeps_extension_when_stem_is_all_unicode() -> None:
    """이름이 통째로 한글이어도 확장자는 살린다 — 클라이언트가 뭘 여는지 알게."""
    assert ascii_fallback("한글.pdf") == "download.pdf"
    assert ascii_fallback("한글") == "download"


def test_content_disposition_has_both_parameters() -> None:
    header = content_disposition("Payroll_서울_2026-07-01~2026-07-15.xlsx")
    assert header.startswith(
        'attachment; filename="Payroll_2026-07-01~2026-07-15.xlsx"'
    )
    assert "filename*=UTF-8''" in header
    assert "%EC%84%9C%EC%9A%B8" in header  # '서울' 퍼센트 인코딩
    # 헤더는 latin-1 로 나간다 — 여기서 인코딩되지 않으면 런타임 500
    header.encode("latin-1")


def test_content_disposition_escapes_quotes() -> None:
    header = content_disposition('we"ird.pdf')
    assert 'filename="weird.pdf"' in header
    header.encode("latin-1")


# ---------------------------------------------------------------------------
# export_filename — 콘솔 export 공통 파일명 규칙
# ---------------------------------------------------------------------------

_AT = datetime(2026, 8, 20, 13, 52, tzinfo=timezone.utc)


def test_export_filename_full_shape() -> None:
    """{Kind}_{스코프}_{범위}_{extra}_{생성시각}.{ext} 순서 고정."""
    assert export_filename(
        "Attendance",
        scope="Downtown",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
        extra=["numbered"],
        generated_at=_AT,
    ) == "Attendance_Downtown_20260801-0815_numbered_20260820-1352Z.xlsx"


def test_export_filename_stamps_even_without_scope_or_range() -> None:
    """조건이 하나도 없어도 생성시각은 붙는다 — 같은 파일명 두 개가 안 생긴다."""
    assert export_filename("Dashboard", generated_at=_AT) == (
        "Dashboard_20260820-1352Z.xlsx"
    )


def test_export_filename_keeps_unicode_scope() -> None:
    """한글 매장명은 파일명에 그대로 (전송은 filename* 담당)."""
    assert export_filename("Staff", scope="서울 2호점", generated_at=_AT) == (
        "Staff_서울2호점_20260820-1352Z.xlsx"
    )


def test_export_filename_range_omitted_when_incomplete() -> None:
    """한쪽 날짜만 있으면 범위를 적지 않는다 — 내용보다 좁아 보이면 안 된다."""
    name = export_filename(
        "Dashboard", start_date=date(2026, 8, 1), end_date=None, generated_at=_AT
    )
    assert name == "Dashboard_20260820-1352Z.xlsx"


def test_export_filename_extra_is_sanitized_and_skips_blanks() -> None:
    name = export_filename(
        "EmpID_Export", extra=["by store", "", "12rows"], generated_at=_AT
    )
    assert name == "EmpID_Export_bystore_12rows_20260820-1352Z.xlsx"


def test_export_filename_honours_ext() -> None:
    assert export_filename("PayStub", ext="pdf", generated_at=_AT).endswith(".pdf")


def test_date_range_tag_shortens_same_year() -> None:
    assert date_range_tag(date(2026, 8, 1), date(2026, 8, 15)) == "20260801-0815"
    assert date_range_tag(date(2025, 12, 30), date(2026, 1, 2)) == (
        "20251230-20260102"
    )
