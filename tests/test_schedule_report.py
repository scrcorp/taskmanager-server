"""스케줄 일일 리포트 — diff / serialization 단위 테스트.

Detection (DB 의존)은 통합 테스트에서 다룸. 여기서는 pure-Python 로직만.
"""

from app.services.schedule_report_service import Issue, ReportDiff, diff_issues
from app.utils.email_templates import build_schedule_daily_report_email
from datetime import date


def _make_issue(key: str, category: str = "sv_missing", target_date: str = "2026-05-16") -> Issue:
    return Issue(
        key=key,
        category=category,
        target_date=target_date,
        label=f"label-{key}",
        store_id="s1",
        store_name="Store 1",
        shift_id="sh1",
        shift_name="Morning",
        user_id=None,
        user_name=None,
        detail={},
    )


def test_diff_issues_new_resolved_ongoing():
    prev = [_make_issue("a"), _make_issue("b")]
    curr = [_make_issue("b"), _make_issue("c")]
    d = diff_issues(prev, curr)
    assert {i.key for i in d.new} == {"c"}
    assert {i.key for i in d.resolved} == {"a"}
    assert {i.key for i in d.ongoing} == {"b"}


def test_diff_issues_empty_inputs():
    d = diff_issues([], [])
    assert d.new == [] and d.resolved == [] and d.ongoing == []

    d = diff_issues([], [_make_issue("x")])
    assert [i.key for i in d.new] == ["x"]
    assert d.resolved == [] and d.ongoing == []

    d = diff_issues([_make_issue("x")], [])
    assert [i.key for i in d.resolved] == ["x"]
    assert d.new == [] and d.ongoing == []


def test_issue_jsonable_roundtrip():
    orig = _make_issue("k1")
    payload = orig.to_jsonable()
    restored = Issue.from_jsonable(payload)
    assert restored == orig


def _make_cell(date_obj: date, shift_name: str = "Morning", sort_order: int = 0,
               staff_count: int = 2, sv_count: int = 1, store_id: str = "s1",
               store_name: str = "Store 1") -> "object":
    from app.services.schedule_report_service import ShiftCell
    return ShiftCell(
        store_id=store_id,
        store_name=store_name,
        shift_id="sh1" if shift_name == "Morning" else f"sh-{shift_name}",
        shift_name=shift_name,
        shift_sort_order=sort_order,
        target_date=date_obj,
        staff_count=staff_count,
        sv_count=sv_count,
    )


def _make_store(sid: str = "s1", name: str = "Store 1"):
    from app.services.schedule_report_service import StoreInfo
    return StoreInfo(id=sid, name=name)


def test_email_renders_with_diff_sections():
    d1, d2 = date(2026, 5, 15), date(2026, 5, 16)
    diff = ReportDiff(
        new=[_make_issue("a", "sv_missing", "2026-05-15")],
        resolved=[_make_issue("b", "shift_understaffed", "2026-05-15")],
        ongoing=[_make_issue("c", "over_6h", "2026-05-16")],
    )
    cells = [
        _make_cell(d1, "Morning", 0, staff_count=2, sv_count=0),
        _make_cell(d1, "Closing", 1, staff_count=3, sv_count=1),
        _make_cell(d2, "Morning", 0, staff_count=3, sv_count=1),
    ]
    subject, html = build_schedule_daily_report_email(
        org_name="Acme",
        sent_date=d1,
        target_dates=[d1, d2],
        diff=diff,
        stores=[_make_store()],
        cells=cells,
        admin_base_url="https://console.example.com",
    )
    assert "Acme" in subject
    # 큰 섹션 헤더 4개
    assert "SECTION 1" in html and "Staffing by Shift" in html
    assert "SECTION 2" in html and "Supervisor Coverage" in html
    assert "SECTION 3" in html and "Overtime" in html
    assert "SECTION 4" in html and "No Break" in html
    # planned/actual 서브그룹
    assert "Planned (schedule)" in html
    assert "Actual (attendance)" in html
    # 매트릭스 헤더
    assert "Store 1" in html
    # 콘솔 링크
    assert "https://console.example.com/schedules" in html


def test_email_renders_empty_stores():
    diff = ReportDiff(new=[], resolved=[], ongoing=[])
    _, html = build_schedule_daily_report_email(
        org_name="Acme",
        sent_date=date(2026, 5, 15),
        target_dates=[date(2026, 5, 15)],
        yesterday=date(2026, 5, 14),
        diff=diff,
        stores=[],
        cells=[],
        admin_base_url="https://console.example.com",
    )
    # 빈 stores 여도 섹션들은 있어야 함
    assert "SECTION 1" in html
    assert "No active stores" in html
    # 날짜별로 "No issues" 표시
    assert "No issues" in html


def test_email_includes_stores_without_shifts():
    """cells 없는 매장도 매트릭스 섹션에 매장명 + 'No shifts configured' 표시."""
    d = date(2026, 5, 15)
    diff = ReportDiff(new=[], resolved=[], ongoing=[])
    _, html = build_schedule_daily_report_email(
        org_name="Acme",
        sent_date=d,
        target_dates=[d],
        yesterday=date(2026, 5, 14),
        diff=diff,
        stores=[_make_store("s1", "Empty Store"), _make_store("s2", "Active Store")],
        cells=[_make_cell(d, "Morning", 0, staff_count=3, sv_count=1, store_id="s2", store_name="Active Store")],
        admin_base_url="https://console.example.com",
    )
    assert "Empty Store" in html
    assert "No shifts configured" in html
    assert "Active Store" in html


def test_email_attendance_section_renders():
    """att_over_6h 카테고리가 actual 서브그룹에 표시되는지."""
    d = date(2026, 5, 15)
    yest = date(2026, 5, 14)
    from app.services.schedule_report_service import Issue
    actual_issue = Issue(
        key="att_over_6h|u1|2026-05-14",
        category="att_over_6h",
        target_date=yest.isoformat(),
        label="Jane (Store 1): 7.5h actual",
        store_id="s1",
        store_name="Store 1",
        shift_id=None,
        shift_name=None,
        user_id="u1",
        user_name="Jane",
        detail={"total_minutes": 450, "source": "attendance"},
    )
    diff = ReportDiff(new=[actual_issue], resolved=[], ongoing=[])
    _, html = build_schedule_daily_report_email(
        org_name="Acme",
        sent_date=d,
        target_dates=[d],
        yesterday=yest,
        diff=diff,
        stores=[_make_store()],
        cells=[],
        admin_base_url="https://console.example.com",
    )
    assert "Actual (attendance)" in html
    assert "Jane" in html
    assert "2026-05-14" in html
